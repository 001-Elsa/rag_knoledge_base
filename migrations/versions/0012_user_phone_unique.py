"""Add canonical unique phone identity to users.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing accounts remain NULL; every new registration supplies a phone.
    # PostgreSQL's UNIQUE semantics allow multiple legacy NULL values while
    # rejecting every duplicate non-NULL phone, including concurrent inserts.
    op.add_column("users", sa.Column("phone", sa.String(length=11), nullable=True))
    op.create_unique_constraint("uq_users_phone", "users", ["phone"])


def downgrade() -> None:
    op.drop_constraint("uq_users_phone", "users", type_="unique")
    op.drop_column("users", "phone")
