"""Revoke UPDATE/DELETE on audit_logs from rag_app — append-only enforcement.

Item 17: ensures the application role (rag_app) can only INSERT and SELECT audit_logs.
The hash-chain trigger already guarantees tamper evidence; this migration adds a
defense-in-depth layer so that even a compromised application session cannot alter or
remove audit records. Only rag_worker (BYPASSRLS) and the admin role retain write
privileges for maintenance-purge operations.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-29
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            REVOKE UPDATE, DELETE ON audit_logs FROM rag_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT UPDATE, DELETE ON audit_logs TO rag_app;
          END IF;
        END $$;
        """
    )
