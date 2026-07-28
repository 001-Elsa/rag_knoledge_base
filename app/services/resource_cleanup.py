"""Coordinate database deletion with object cleanup and cache invalidation."""
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocStatus, Document, KnowledgeBase
from app.services import semantic_cache
from app.services.object_storage import get_object_storage

logger = logging.getLogger(__name__)


def _remove_objects(keys: list[str]) -> None:
    storage = get_object_storage()
    for object_key in keys:
        try:
            storage.delete(object_key)
        except Exception:
            logger.warning("Failed to remove document object: %s", object_key, exc_info=True)


async def delete_document(db: AsyncSession, document: Document, owner_id: str) -> None:
    """Delete one document, then clean up resources outside the DB transaction."""
    object_key = document.object_key
    document.status = DocStatus.deleting
    await db.flush()
    await db.delete(document)
    await db.commit()
    await asyncio.to_thread(_remove_objects, [object_key])
    await semantic_cache.invalidate_kb(document.kb_id)


async def delete_knowledge_base(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    owner_id: str,
) -> None:
    """Delete a knowledge base, its files, and answers cached from its content."""
    object_keys = list(
        (
            await db.execute(
                select(Document.object_key).where(Document.kb_id == knowledge_base.id)
            )
        ).scalars()
    )
    await db.delete(knowledge_base)
    await db.commit()
    await asyncio.to_thread(_remove_objects, object_keys)
    await semantic_cache.invalidate_kb(knowledge_base.id)
