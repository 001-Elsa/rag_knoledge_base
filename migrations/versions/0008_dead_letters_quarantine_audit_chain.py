"""Dead-letter tasks, document quarantine, and audit-log hash chain.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Column expression shared by the trigger, the backfill, and the verifier script so
# the chain can be recomputed deterministically. `#>> '{}'` normalises JSON to text.
# {prev} is passed separately: inside UPDATE, column references see OLD values.
_HASH_EXPR = (
    "encode(digest("
    "{prev} || '|' || {p}.id || '|' || {p}.action || '|' || "
    "coalesce({p}.resource_type, '') || '|' || coalesce({p}.resource_id, '') || '|' || "
    "coalesce({p}.actor_user_id, '') || '|' || coalesce({p}.workspace_id, '') || '|' || "
    "coalesce({p}.outcome, '') || '|' || coalesce({p}.before #>> '{{}}', '') || '|' || "
    "coalesce({p}.after #>> '{{}}', '') || '|' || {p}.created_at::text, "
    "'sha256'), 'hex')"
)


def upgrade() -> None:
    # ---- Prompt-injection quarantine and ingestion checkpoint fingerprint ----
    op.add_column(
        "documents",
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "documents",
        sa.Column("pipeline_fingerprint", sa.String(64), nullable=True),
    )

    # ---- Dead letter queue for terminally failed asynchronous work ----
    op.execute(
        "CREATE TYPE deadletterstatus AS ENUM ('pending','replayed','discarded')"
    )
    dead_letter_status = postgresql.ENUM(
        "pending", "replayed", "discarded", name="deadletterstatus", create_type=False
    )
    op.create_table(
        "dead_letter_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("task_name", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(32), nullable=True),
        sa.Column("kb_id", sa.String(32), nullable=True),
        sa.Column("workspace_id", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("failed_stage", sa.String(32), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", dead_letter_status, nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(32), nullable=True),
    )
    for column in ("source", "task_name", "document_id", "workspace_id", "status"):
        op.create_index(
            f"ix_dead_letter_tasks_{column}", "dead_letter_tasks", [column]
        )

    # Defense in depth: only workspace owner/admin roles may read or resolve dead
    # letters through the RLS-constrained application role. Workers use rag_worker
    # (BYPASSRLS) to insert rows.
    op.execute("ALTER TABLE dead_letter_tasks ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY dead_letter_admin_isolation ON dead_letter_tasks
        USING (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = dead_letter_tasks.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin')
        ))
        WITH CHECK (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = dead_letter_tasks.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin')
        ))
        """
    )

    # ---- Tamper-evident audit hash chain ----
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # BIGSERIAL attaches an owned sequence and a default in one statement.
    op.execute("ALTER TABLE audit_logs ADD COLUMN chain_seq BIGSERIAL")
    op.add_column("audit_logs", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("entry_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_audit_logs_chain_seq", "audit_logs", ["chain_seq"], unique=True
    )

    # Backfill the chain for existing rows in chain_seq order. The append-only
    # trigger requires the transaction-local maintenance flag for UPDATE.
    op.execute("SELECT set_config('app.audit_maintenance', 'on', true)")
    op.execute(
        f"""
        DO $$
        DECLARE
          audit RECORD;
          previous text := repeat('0', 64);
        BEGIN
          PERFORM set_config('app.audit_maintenance', 'on', true);
          FOR audit IN SELECT * FROM audit_logs ORDER BY chain_seq LOOP
            UPDATE audit_logs SET
              prev_hash = previous,
              entry_hash = {_HASH_EXPR.format(prev="previous", p="audit_logs")}
            WHERE id = audit.id;
            SELECT entry_hash INTO previous FROM audit_logs WHERE id = audit.id;
          END LOOP;
        END $$;
        """
    )

    # The advisory transaction lock serialises audit inserts so that every row links
    # to the previously committed row. Throughput cost is accepted for tamper
    # evidence; audit volume is low relative to business writes.
    op.execute(
        f"""
        CREATE FUNCTION audit_logs_hash_chain() RETURNS trigger AS $$
        DECLARE
          previous_hash text;
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtext('audit_logs_hash_chain'));
          SELECT entry_hash INTO previous_hash
          FROM audit_logs ORDER BY chain_seq DESC LIMIT 1;
          NEW.prev_hash := coalesce(previous_hash, repeat('0', 64));
          NEW.entry_hash := {_HASH_EXPR.format(prev="NEW.prev_hash", p="NEW")};
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_hash_chain
        BEFORE INSERT ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_hash_chain()
        """
    )

    # Roles created by deploy/init-db.sh need access to the new table and sequence.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON dead_letter_tasks TO rag_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_worker') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON dead_letter_tasks TO rag_worker;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_worker;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_hash_chain ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_hash_chain")
    op.drop_index("ix_audit_logs_chain_seq", table_name="audit_logs")
    op.drop_column("audit_logs", "entry_hash")
    op.drop_column("audit_logs", "prev_hash")
    op.drop_column("audit_logs", "chain_seq")
    op.drop_table("dead_letter_tasks")
    op.execute("DROP TYPE IF EXISTS deadletterstatus")
    op.drop_column("documents", "pipeline_fingerprint")
    op.drop_column("documents", "quarantined")
