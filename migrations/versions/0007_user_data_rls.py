"""RLS for conversations, messages, and usage records.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("conversations", "messages", "usage_records"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY conversation_owner_isolation ON conversations
        USING (owner_id = current_setting('app.user_id', true))
        WITH CHECK (owner_id = current_setting('app.user_id', true))
        """
    )
    op.execute(
        """
        CREATE POLICY message_owner_isolation ON messages
        USING (EXISTS (
          SELECT 1 FROM conversations c
          WHERE c.id = messages.conversation_id
            AND c.owner_id = current_setting('app.user_id', true)
        ))
        WITH CHECK (EXISTS (
          SELECT 1 FROM conversations c
          WHERE c.id = messages.conversation_id
            AND c.owner_id = current_setting('app.user_id', true)
        ))
        """
    )
    op.execute(
        """
        CREATE POLICY usage_owner_isolation ON usage_records
        USING (owner_id = current_setting('app.user_id', true))
        WITH CHECK (owner_id = current_setting('app.user_id', true))
        """
    )


def downgrade() -> None:
    for table, policy in (
        ("usage_records", "usage_owner_isolation"),
        ("messages", "message_owner_isolation"),
        ("conversations", "conversation_owner_isolation"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
