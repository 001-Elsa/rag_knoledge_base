"""Parent-child retrieval metadata.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("parent_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("chunks", sa.Column("parent_content", sa.Text(), nullable=True))
    op.execute("UPDATE chunks SET parent_content = content WHERE parent_content IS NULL")
    op.create_index(
        "ix_chunks_parent",
        "chunks",
        ["document_id", "index_version", "parent_seq"],
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_worker') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO rag_worker;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_parent", table_name="chunks")
    op.drop_column("chunks", "parent_content")
    op.drop_column("chunks", "parent_seq")
