"""Coordinate database deletion with outbox-backed object cleanup.

Item 13: strict atomic delete is not available across DB + object storage, so we
use eventual consistency with a durable outbox:

  mark deleting → write resource.delete OutboxEvent (same transaction)
  → dispatcher → delete_resources worker
  → storage delete → hard-delete / mark deleted
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocStatus, Document, KnowledgeBase, OutboxEvent
from app.services import semantic_cache

logger = logging.getLogger(__name__)


async def delete_document(db: AsyncSession, document: Document, owner_id: str) -> None:
    """Mark deleting and enqueue reliable object deletion; do not best-effort delete."""
    object_key = document.object_key
    kb_id = document.kb_id
    kb = await db.get(KnowledgeBase, kb_id)
    workspace_id = kb.workspace_id if kb else None
    document.status = DocStatus.deleting
    document.stage = DocStatus.deleting.value
    document.processing_token = None
    document.worker_id = None
    event_id = uuid.uuid4().hex
    db.add(
        OutboxEvent(
            id=event_id,
            event_type="resource.delete.requested",
            aggregate_type="document",
            aggregate_id=document.id,
            payload={
                "document_id": document.id,
                "kb_id": kb_id,
                "workspace_id": workspace_id,
                "object_keys": [object_key],
                "requested_by": owner_id,
            },
            dedup_key=f"resource.delete.document:{document.id}:{event_id}",
        )
    )
    await db.commit()
    # Cache can be invalidated immediately; retrieval already skips deleting status
    # via active_index_version / status filters once chunks are gone after worker.
    await semantic_cache.invalidate_kb(kb_id)


async def delete_knowledge_base(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    owner_id: str,
) -> None:
    """Mark all docs deleting, enqueue one resource.delete for all object keys, drop KB."""
    docs = list(
        (
            await db.execute(
                select(Document).where(Document.kb_id == knowledge_base.id)
            )
        ).scalars()
    )
    object_keys = [doc.object_key for doc in docs if doc.object_key]
    for document in docs:
        document.status = DocStatus.deleting
        document.stage = DocStatus.deleting.value
        document.processing_token = None
        document.worker_id = None
    kb_id = knowledge_base.id
    workspace_id = knowledge_base.workspace_id
    event_id = uuid.uuid4().hex
    if object_keys:
        db.add(
            OutboxEvent(
                id=event_id,
                event_type="resource.delete.requested",
                aggregate_type="knowledge_base",
                aggregate_id=kb_id,
                payload={
                    "kb_id": kb_id,
                    "workspace_id": workspace_id,
                    "object_keys": object_keys,
                    "requested_by": owner_id,
                },
                dedup_key=f"resource.delete.kb:{kb_id}:{event_id}",
            )
        )
    # Cascade-delete documents + chunks with the KB; objects are deleted asynchronously.
    await db.delete(knowledge_base)
    await db.commit()
    await semantic_cache.invalidate_kb(kb_id)
