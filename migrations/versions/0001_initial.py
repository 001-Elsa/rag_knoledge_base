"""初始建表：用户/知识库/文档/切片(向量)/会话/消息/用量记录

Revision ID: 0001
Revises:
Create Date: 2026-07-26
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 512  # 与 app/config.py 的 embedding_dim 保持一致（bge-small-zh-v1.5）

docstatus = sa.Enum("pending", "processing", "ready", "failed", name="docstatus")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_bases_owner_id", "knowledge_bases", ["owner_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("kb_id", sa.String(32), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("filepath", sa.String(512), nullable=False),
        sa.Column("status", docstatus, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
    op.create_index("ix_documents_kb_id", "documents", ["kb_id"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("kb_id", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_owner_id", "chunks", ["owner_id"])
    op.create_index("ix_chunks_kb_id", "chunks", ["kb_id"])
    # HNSW 近似最近邻索引（余弦距离）
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kb_id", sa.String(32), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("first_token_ms", sa.Integer(), nullable=False),
        sa.Column("total_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_records_owner_id", "usage_records", ["owner_id"])
    op.create_index("ix_usage_records_created_at", "usage_records", ["created_at"])


def downgrade() -> None:
    for table in ("usage_records", "messages", "conversations", "chunks", "documents", "knowledge_bases", "users"):
        op.drop_table(table)
    docstatus.drop(op.get_bind(), checkfirst=True)
