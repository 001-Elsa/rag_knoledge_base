"""全文检索（tsvector + GIN）与会话滚动摘要字段

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26

说明：
- chunks.content_tokens：jieba 分词后的 tsvector，配 GIN 倒排索引，
  关键词召回从 LIKE 全表扫描升级为倒排索引查询；
- 本迁移不回填旧数据（jieba 分词需在 Python 侧完成）——
  存量切片请执行 `python scripts/backfill_tokens.py`，或重新上传文档。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("content_tokens", postgresql.TSVECTOR(), nullable=True))
    op.execute("CREATE INDEX ix_chunks_content_tokens ON chunks USING gin (content_tokens)")

    op.add_column("conversations", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("summary_upto", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "summary_upto")
    op.drop_column("conversations", "summary")
    op.drop_index("ix_chunks_content_tokens", table_name="chunks")
    op.drop_column("chunks", "content_tokens")
