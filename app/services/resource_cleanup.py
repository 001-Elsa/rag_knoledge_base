"""Coordinate database deletion with file cleanup and cache invalidation."""
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, KnowledgeBase
from app.services import semantic_cache

logger = logging.getLogger(__name__)


def _remove_files(paths: list[str]) -> None:
    for raw_path in paths:
        try:
            Path(raw_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove document file: %s", raw_path, exc_info=True)


async def delete_document(db: AsyncSession, document: Document, owner_id: str) -> None:
    """Delete one document, then clean up resources outside the DB transaction."""
    filepath = document.filepath
    await db.delete(document)
    await db.commit()
    _remove_files([filepath])
    await semantic_cache.invalidate_user(owner_id)


async def delete_knowledge_base(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    owner_id: str,
) -> None:
    """Delete a knowledge base, its files, and answers cached from its content."""
    filepaths = list(
        (
            await db.execute(
                select(Document.filepath).where(Document.kb_id == knowledge_base.id)
            )
        ).scalars()
    )
    await db.delete(knowledge_base)
    await db.commit()
    _remove_files(filepaths)
    await semantic_cache.invalidate_user(owner_id)
