"""Tenant-scoped database coordination helpers."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_workspace_quota(db: AsyncSession, workspace_id: str) -> None:
    """Serialize quota check + reservation for one workspace until transaction end."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"workspace-storage:{workspace_id}"},
    )
