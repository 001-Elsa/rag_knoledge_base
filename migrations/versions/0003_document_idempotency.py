"""Add document content hashes and ingestion uniqueness constraints.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_documents_owner_kb_content_hash",
        "documents",
        ["owner_id", "kb_id", "content_hash"],
    )
    op.create_unique_constraint(
        "uq_chunks_document_seq",
        "chunks",
        ["document_id", "seq"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_chunks_document_seq", "chunks", type_="unique")
    op.drop_constraint("uq_documents_owner_kb_content_hash", "documents", type_="unique")
    op.drop_column("documents", "content_hash")
