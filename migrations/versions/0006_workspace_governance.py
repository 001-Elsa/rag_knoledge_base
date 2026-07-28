"""Workspace naming constraint.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_workspace_org_name", "workspaces", ["organization_id", "name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_workspace_org_name", "workspaces", type_="unique")
