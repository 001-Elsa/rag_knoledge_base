"""Reconciliation jobs for crashed workers and superseded index versions."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update

from app.config import settings
from app.db import SyncSessionLocal
from app.models import AuditLog, Chunk, DocStatus, Document, OutboxEvent
from app.services.object_storage import get_object_storage
from app.tasks import celery_app


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


@celery_app.task(name="reconcile_stale_ingestion")
def reconcile_stale_ingestion(stale_after_seconds: int = 300) -> dict:
    cutoff = _now() - timedelta(seconds=stale_after_seconds)
    active = [
        DocStatus.parsing,
        DocStatus.chunking,
        DocStatus.embedding,
        DocStatus.indexing,
    ]
    with SyncSessionLocal() as db:
        stale_ids = list(
            db.execute(
                update(Document)
                .where(
                    Document.status.in_(active),
                    Document.heartbeat_at < cutoff,
                )
                .values(
                    status=DocStatus.retrying,
                    stage=DocStatus.retrying.value,
                    processing_token=None,
                    worker_id=None,
                    error="worker heartbeat expired; reconciler scheduled retry",
                )
                .returning(Document.id)
            ).scalars()
        )
        for document_id in stale_ids:
            event = OutboxEvent(
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document_id,
                payload={"reason": "stale-worker-recovery"},
                dedup_key=f"document.ingest.recovery:{document_id}:{_now().timestamp()}",
            )
            db.add(event)
        db.commit()
    return {"recovered": len(stale_ids)}


@celery_app.task(name="cleanup_superseded_indexes")
def cleanup_superseded_indexes(document_id: str) -> dict:
    with SyncSessionLocal() as db:
        document = db.get(Document, document_id)
        if document is None or document.active_index_version is None:
            return {"deleted": 0}
        result = db.execute(
            delete(Chunk).where(
                Chunk.document_id == document_id,
                Chunk.index_version != document.active_index_version,
            )
        )
        db.commit()
        return {"deleted": result.rowcount or 0}


@celery_app.task(name="reconcile_orphan_objects")
def reconcile_orphan_objects(dry_run: bool = True) -> dict:
    """Delete storage objects absent from DB only after a safety grace period."""
    with SyncSessionLocal() as db:
        referenced = set(db.execute(select(Document.object_key)).scalars())
    cutoff = _now() - timedelta(seconds=settings.orphan_object_grace_seconds)
    storage = get_object_storage()
    orphans = [
        item
        for item in storage.list_objects()
        if item.key not in referenced and item.last_modified < cutoff
    ]
    if not dry_run:
        for item in orphans:
            storage.delete(item.key)
    return {
        "dry_run": dry_run,
        "orphan_count": len(orphans),
        "object_keys": [item.key for item in orphans[:100]],
    }


@celery_app.task(name="purge_expired_audit_logs")
def purge_expired_audit_logs() -> dict:
    cutoff = _now() - timedelta(days=settings.audit_retention_days)
    with SyncSessionLocal() as db:
        db.execute(text("SELECT set_config('app.audit_maintenance', 'on', true)"))
        result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        db.commit()
        return {"deleted": result.rowcount or 0, "retention_days": settings.audit_retention_days}
