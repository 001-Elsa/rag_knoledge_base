"""Advanced RAG metadata, feedback, and lightweight knowledge graph.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_records", sa.Column("estimated_cost_microusd", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "api_key_credentials",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_key_credentials_owner_id", "api_key_credentials", ["owner_id"])
    op.create_index("ix_api_key_credentials_prefix", "api_key_credentials", ["prefix"])
    op.create_index("ix_api_key_credentials_secret_hash", "api_key_credentials", ["secret_hash"], unique=True)
    op.add_column("documents", sa.Column("source_type", sa.String(32), nullable=False, server_default="upload"))
    op.add_column("documents", sa.Column("source_url", sa.String(2048), nullable=True))
    op.add_column("documents", sa.Column("source_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("documents", sa.Column("department", sa.String(128), nullable=True))
    op.add_column("documents", sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")))
    op.create_index("ix_documents_department", "documents", ["department"])
    op.add_column("documents", sa.Column("language", sa.String(32), nullable=True))
    op.add_column("documents", sa.Column("embedding_dim", sa.Integer(), nullable=True))

    op.add_column("chunks", sa.Column("workspace_id", sa.String(32), nullable=True))
    op.add_column("chunks", sa.Column("section", sa.String(512), nullable=True))
    op.add_column("chunks", sa.Column("content_type", sa.String(32), nullable=False, server_default="text"))
    op.add_column("chunks", sa.Column("source_url", sa.String(2048), nullable=True))
    op.add_column("chunks", sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chunks", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("chunks", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.execute(
        "UPDATE chunks SET workspace_id = kb.workspace_id FROM knowledge_bases kb WHERE kb.id = chunks.kb_id"
    )
    op.alter_column("chunks", "workspace_id", nullable=False)
    op.create_index("ix_chunks_workspace_id", "chunks", ["workspace_id"])
    op.create_index("ix_chunks_metadata_filter", "chunks", ["workspace_id", "content_type", "created_at"])

    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("comment", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_answer_feedback_rating"),
    )
    op.create_index("ix_answer_feedback_owner_id", "answer_feedback", ["owner_id"])
    op.create_index("ix_answer_feedback_conversation_id", "answer_feedback", ["conversation_id"])
    op.create_index("ix_answer_feedback_message_id", "answer_feedback", ["message_id"])
    op.create_index("ix_answer_feedback_created_at", "answer_feedback", ["created_at"])

    op.create_table(
        "graph_entities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("kb_id", sa.String(32), nullable=False),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False, server_default="keyword"),
        sa.Column("chunk_id", sa.String(32), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "normalized_name", "chunk_id", name="uq_graph_entity_document_chunk"),
    )
    for name, columns in {
        "ix_graph_entities_workspace_id": ["workspace_id"],
        "ix_graph_entities_kb_id": ["kb_id"],
        "ix_graph_entities_document_id": ["document_id"],
        "ix_graph_entities_normalized_name": ["normalized_name"],
        "ix_graph_entities_chunk_id": ["chunk_id"],
    }.items():
        op.create_index(name, "graph_entities", columns)

    op.create_table(
        "graph_relations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("kb_id", sa.String(32), nullable=False),
        sa.Column("source_entity_id", sa.String(32), sa.ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_entity_id", sa.String(32), sa.ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False, server_default="co_occurs"),
        sa.Column("evidence_chunk_id", sa.String(32), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_entity_id", "target_entity_id", "relation_type", "evidence_chunk_id", name="uq_graph_relation_evidence"),
    )
    for name, columns in {
        "ix_graph_relations_workspace_id": ["workspace_id"],
        "ix_graph_relations_kb_id": ["kb_id"],
        "ix_graph_relations_source_entity_id": ["source_entity_id"],
        "ix_graph_relations_target_entity_id": ["target_entity_id"],
        "ix_graph_relations_evidence_chunk_id": ["evidence_chunk_id"],
    }.items():
        op.create_index(name, "graph_relations", columns)

    for table in ("answer_feedback", "graph_entities", "graph_relations"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY answer_feedback_owner ON answer_feedback USING (owner_id = current_setting('app.user_id', true)) WITH CHECK (owner_id = current_setting('app.user_id', true))"
    )
    for table in ("graph_entities", "graph_relations"):
        op.execute(
            f"CREATE POLICY {table}_workspace ON {table} USING (EXISTS (SELECT 1 FROM workspace_memberships m WHERE m.workspace_id = {table}.workspace_id AND m.user_id = current_setting('app.user_id', true))) WITH CHECK (EXISTS (SELECT 1 FROM workspace_memberships m WHERE m.workspace_id = {table}.workspace_id AND m.user_id = current_setting('app.user_id', true)))"
        )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON api_key_credentials, answer_feedback, graph_entities, graph_relations TO rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_worker') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON api_key_credentials, answer_feedback, graph_entities, graph_relations TO rag_worker;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_column("usage_records", "estimated_cost_microusd")
    op.drop_table("graph_relations")
    op.drop_table("graph_entities")
    op.drop_table("answer_feedback")
    op.drop_table("api_key_credentials")
    op.drop_index("ix_chunks_metadata_filter", table_name="chunks")
    op.drop_index("ix_chunks_workspace_id", table_name="chunks")
    for column in ("created_at", "metadata_json", "token_count", "source_url", "content_type", "section", "workspace_id"):
        op.drop_column("chunks", column)
    op.drop_index("ix_documents_department", table_name="documents")
    for column in ("embedding_dim", "language", "tags", "department", "source_metadata", "source_url", "source_type"):
        op.drop_column("documents", column)
