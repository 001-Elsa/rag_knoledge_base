"""Reliable ingestion, versioned indexes, tenancy, RBAC, RLS, and audit.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # A nullable hash makes PostgreSQL's unique constraint ineffective for NULL rows.
    # Refuse to invent hashes: operators must run scripts/backfill_document_hashes.py first.
    null_hashes = bind.execute(
        sa.text("SELECT count(*) FROM documents WHERE content_hash IS NULL")
    ).scalar_one()
    if null_hashes:
        raise RuntimeError(
            f"{null_hashes} documents have no content_hash; "
            "run `python scripts/backfill_document_hashes.py` before this migration"
        )

    op.alter_column("documents", "content_hash", existing_type=sa.String(64), nullable=False)

    # Replace the four-state enum in one controlled conversion.
    op.execute(
        "CREATE TYPE docstatus_v2 AS ENUM ("
        "'uploaded','queued','parsing','chunking','embedding','indexing','ready',"
        "'retrying','failed','cancelled','deleting','deleted')"
    )
    op.execute("ALTER TABLE documents ALTER COLUMN status DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE documents
        ALTER COLUMN status TYPE docstatus_v2
        USING (
            CASE status::text
                WHEN 'pending' THEN 'uploaded'
                WHEN 'processing' THEN 'retrying'
                ELSE status::text
            END
        )::docstatus_v2
        """
    )
    op.execute("DROP TYPE docstatus")
    op.execute("ALTER TYPE docstatus_v2 RENAME TO docstatus")

    workspace_role = postgresql.ENUM(
        "owner",
        "admin",
        "editor",
        "viewer",
        "auditor",
        name="workspacerole",
        create_type=False,
    )
    outbox_status = postgresql.ENUM(
        "pending",
        "publishing",
        "sent",
        "failed",
        name="outboxstatus",
        create_type=False,
    )
    op.execute("CREATE TYPE workspacerole AS ENUM ('owner','admin','editor','viewer','auditor')")
    op.execute("CREATE TYPE outboxstatus AS ENUM ('pending','publishing','sent','failed')")

    op.create_table(
        "organizations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "created_by",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_organizations_created_by", "organizations", ["created_by"])

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(32),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])

    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(32),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", workspace_role, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
    )
    op.create_index("ix_workspace_memberships_workspace_id", "workspace_memberships", ["workspace_id"])
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])

    # Existing users receive a personal organization/workspace without changing ownership.
    op.execute(
        """
        INSERT INTO organizations (id, name, created_by, created_at)
        SELECT md5('org:' || id), username || ' organization', id, created_at FROM users
        """
    )
    op.execute(
        """
        INSERT INTO workspaces (id, organization_id, name, created_at)
        SELECT md5('workspace:' || id), md5('org:' || id), 'Personal', created_at FROM users
        """
    )
    op.execute(
        """
        INSERT INTO workspace_memberships (id, workspace_id, user_id, role, created_at)
        SELECT md5('membership:' || id), md5('workspace:' || id), id, 'owner', created_at FROM users
        """
    )

    op.add_column("knowledge_bases", sa.Column("workspace_id", sa.String(32), nullable=True))
    op.execute(
        "UPDATE knowledge_bases SET workspace_id = md5('workspace:' || owner_id) "
        "WHERE workspace_id IS NULL"
    )
    op.alter_column("knowledge_bases", "workspace_id", nullable=False)
    op.create_foreign_key(
        "fk_knowledge_bases_workspace",
        "knowledge_bases",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_knowledge_bases_workspace_id", "knowledge_bases", ["workspace_id"])
    op.create_unique_constraint("uq_kb_workspace_name", "knowledge_bases", ["workspace_id", "name"])

    document_columns = [
        sa.Column("object_key", sa.String(768), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="uploaded"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processing_token", sa.String(64), nullable=True),
        sa.Column("worker_id", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_index_version", sa.Integer(), nullable=True),
        sa.Column("target_index_version", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("chunk_strategy", sa.String(64), nullable=False, server_default="recursive"),
        sa.Column("chunk_config_hash", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in document_columns:
        op.add_column("documents", column)
    op.execute("UPDATE documents SET object_key = filepath, updated_at = created_at")
    op.execute("UPDATE documents SET active_index_version = 1 WHERE status = 'ready'")
    op.alter_column("documents", "object_key", nullable=False)
    op.alter_column("documents", "updated_at", nullable=False)
    op.create_index("ix_documents_processing_token", "documents", ["processing_token"])

    op.add_column(
        "chunks", sa.Column("index_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.drop_constraint("uq_chunks_document_seq", "chunks", type_="unique")
    op.create_unique_constraint(
        "uq_chunks_document_seq_version",
        "chunks",
        ["document_id", "seq", "index_version"],
    )
    op.create_index(
        "ix_chunks_document_version", "chunks", ["document_id", "index_version"]
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False, unique=True),
        sa.Column("status", outbox_status, nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_next_retry_at", "outbox_events", ["next_retry_at"])
    op.create_index(
        "ix_outbox_dispatch",
        "outbox_events",
        ["status", "next_retry_at", "created_at"],
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=True),
        sa.Column("workspace_id", sa.String(32), nullable=True),
        sa.Column("actor_user_id", sa.String(32), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("source_ip", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    for column in (
        "organization_id",
        "workspace_id",
        "actor_user_id",
        "action",
        "request_id",
        "trace_id",
        "created_at",
    ):
        op.create_index(f"ix_audit_logs_{column}", "audit_logs", [column])

    # Append-only audit records. Maintenance needs an explicit transaction-local override.
    op.execute(
        """
        CREATE FUNCTION prevent_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          IF current_setting('app.audit_maintenance', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION 'audit_logs are append-only';
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_immutable
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_mutation()
        """
    )

    # Defense in depth. Production uses a non-owner application role so these policies apply.
    for table in ("knowledge_bases", "documents", "chunks", "audit_logs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY kb_workspace_isolation ON knowledge_bases
        USING (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = knowledge_bases.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
        ))
        WITH CHECK (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = knowledge_bases.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','editor')
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY document_workspace_isolation ON documents
        USING (EXISTS (
          SELECT 1 FROM knowledge_bases kb
          JOIN workspace_memberships m ON m.workspace_id = kb.workspace_id
          WHERE kb.id = documents.kb_id
            AND m.user_id = current_setting('app.user_id', true)
        ))
        WITH CHECK (EXISTS (
          SELECT 1 FROM knowledge_bases kb
          JOIN workspace_memberships m ON m.workspace_id = kb.workspace_id
          WHERE kb.id = documents.kb_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','editor')
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY chunk_workspace_isolation ON chunks
        USING (EXISTS (
          SELECT 1 FROM knowledge_bases kb
          JOIN workspace_memberships m ON m.workspace_id = kb.workspace_id
          WHERE kb.id = chunks.kb_id
            AND m.user_id = current_setting('app.user_id', true)
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY audit_workspace_isolation ON audit_logs
        FOR SELECT USING (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = audit_logs.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','auditor')
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY audit_append_policy ON audit_logs
        FOR INSERT WITH CHECK (
          actor_user_id = current_setting('app.user_id', true)
          AND EXISTS (
            SELECT 1 FROM workspace_memberships m
            WHERE m.workspace_id = audit_logs.workspace_id
              AND m.user_id = current_setting('app.user_id', true)
          )
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT USAGE ON SCHEMA public TO rag_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_worker') THEN
            GRANT USAGE ON SCHEMA public TO rag_worker;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_worker;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_worker;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for table in ("audit_logs", "chunks", "documents", "knowledge_bases"):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_log_mutation")
    op.drop_table("audit_logs")
    op.drop_table("outbox_events")
    op.drop_index("ix_chunks_document_version", table_name="chunks")
    op.drop_constraint("uq_chunks_document_seq_version", "chunks", type_="unique")
    op.create_unique_constraint("uq_chunks_document_seq", "chunks", ["document_id", "seq"])
    op.drop_column("chunks", "index_version")

    for column in (
        "updated_at",
        "chunk_config_hash",
        "chunk_strategy",
        "embedding_model",
        "target_index_version",
        "active_index_version",
        "heartbeat_at",
        "finished_at",
        "started_at",
        "worker_id",
        "processing_token",
        "retry_count",
        "stage",
        "size_bytes",
        "mime_type",
        "object_key",
    ):
        op.drop_column("documents", column)

    op.drop_constraint("uq_kb_workspace_name", "knowledge_bases", type_="unique")
    op.drop_constraint("fk_knowledge_bases_workspace", "knowledge_bases", type_="foreignkey")
    op.drop_column("knowledge_bases", "workspace_id")
    op.drop_table("workspace_memberships")
    op.drop_table("workspaces")
    op.drop_table("organizations")

    op.alter_column("documents", "content_hash", existing_type=sa.String(64), nullable=True)
    op.execute(
        "CREATE TYPE docstatus_v1 AS ENUM ('pending','processing','ready','failed')"
    )
    op.execute(
        """
        ALTER TABLE documents ALTER COLUMN status TYPE docstatus_v1
        USING (
          CASE
            WHEN status::text IN ('uploaded','queued') THEN 'pending'
            WHEN status::text IN ('parsing','chunking','embedding','indexing','retrying') THEN 'processing'
            WHEN status::text = 'ready' THEN 'ready'
            ELSE 'failed'
          END
        )::docstatus_v1
        """
    )
    op.execute("DROP TYPE docstatus")
    op.execute("ALTER TYPE docstatus_v1 RENAME TO docstatus")
    postgresql.ENUM(name="outboxstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="workspacerole").drop(op.get_bind(), checkfirst=True)
