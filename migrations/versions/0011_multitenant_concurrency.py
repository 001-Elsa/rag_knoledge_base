"""Harden tenant RLS policies and least-privilege content access.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-08
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # rag_app is deliberately not a table owner, but FORCE RLS prevents an
    # accidental future ownership change from silently removing this boundary.
    for table in (
        "knowledge_bases",
        "documents",
        "chunks",
        "audit_logs",
        "conversations",
        "messages",
        "usage_records",
        "dead_letter_tasks",
        "answer_feedback",
        "graph_entities",
        "graph_relations",
    ):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Auditors may inspect audit records but must not retrieve raw knowledge
    # chunks or graph evidence. Application writes are performed by rag_worker.
    op.execute("DROP POLICY IF EXISTS chunk_workspace_isolation ON chunks")
    op.execute(
        """
        CREATE POLICY chunk_workspace_isolation ON chunks
        FOR SELECT USING (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = chunks.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','editor','viewer')
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY chunk_metadata_update ON chunks
        FOR UPDATE USING (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = chunks.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','editor')
        ))
        WITH CHECK (EXISTS (
          SELECT 1 FROM workspace_memberships m
          WHERE m.workspace_id = chunks.workspace_id
            AND m.user_id = current_setting('app.user_id', true)
            AND m.role IN ('owner','admin','editor')
        ))
        """
    )

    for table in ("graph_entities", "graph_relations"):
        op.execute(f"DROP POLICY IF EXISTS {table}_workspace ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_workspace ON {table}
            FOR SELECT USING (EXISTS (
              SELECT 1 FROM workspace_memberships m
              WHERE m.workspace_id = {table}.workspace_id
                AND m.user_id = current_setting('app.user_id', true)
                AND m.role IN ('owner','admin','editor','viewer')
            ))
            """
        )

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app' AND rolbypassrls) THEN
              RAISE EXCEPTION 'rag_app must not have BYPASSRLS';
            END IF;
            REVOKE INSERT, UPDATE, DELETE ON chunks, graph_entities, graph_relations FROM rag_app;
            GRANT UPDATE (source_url) ON chunks TO rag_app;
            REVOKE UPDATE, DELETE ON answer_feedback FROM rag_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS chunk_metadata_update ON chunks")
    op.execute("DROP POLICY IF EXISTS chunk_workspace_isolation ON chunks")
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
    for table in ("graph_entities", "graph_relations"):
        op.execute(f"DROP POLICY IF EXISTS {table}_workspace ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_workspace ON {table}
            USING (EXISTS (
              SELECT 1 FROM workspace_memberships m
              WHERE m.workspace_id = {table}.workspace_id
                AND m.user_id = current_setting('app.user_id', true)
            ))
            WITH CHECK (EXISTS (
              SELECT 1 FROM workspace_memberships m
              WHERE m.workspace_id = {table}.workspace_id
                AND m.user_id = current_setting('app.user_id', true)
            ))
            """
        )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT INSERT, UPDATE, DELETE ON chunks, graph_entities, graph_relations TO rag_app;
            GRANT UPDATE, DELETE ON answer_feedback TO rag_app;
          END IF;
        END $$;
        """
    )
    for table in (
        "dead_letter_tasks",
        "usage_records",
        "messages",
        "conversations",
        "audit_logs",
        "chunks",
        "documents",
        "knowledge_bases",
    ):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
