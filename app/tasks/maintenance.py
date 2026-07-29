"""Reconciliation, reliable resource deletion, audit retention, and DLQ auto-recovery."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, text, update

from app.config import settings
from app.db import SyncSessionLocal
from app.metrics import RESOURCE_DELETE_FAILURES
from app.models import (
    AuditLog,
    Chunk,
    DeadLetterStatus,
    DeadLetterTask,
    DocStatus,
    Document,
    OutboxEvent,
)
from app.services.object_storage import get_object_storage, make_staging_file
from app.tasks import celery_app
from app.tasks.ingest import record_dead_letter

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


@celery_app.task(name="reconcile_stale_ingestion")
def reconcile_stale_ingestion(stale_after_seconds: int = 300) -> dict:
    """Reclaim documents whose worker lease heartbeat expired.

    Long embedding batches refresh heartbeat_at on each checkpoint, so a live
    worker is not reclaimed. Only true stalls (crash / network partition) land
    here and are re-queued through the outbox.
    """
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
        # Objects still referenced by deleting rows (awaiting resource.delete) must
        # not be treated as orphans until the document row itself is gone.
        referenced |= set(
            db.execute(
                select(Document.object_key).where(Document.status == DocStatus.deleting)
            ).scalars()
        )
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


@celery_app.task(
    name="delete_resources",
    bind=True,
    max_retries=8,
    acks_late=True,
    reject_on_worker_lost=True,
)
def delete_resources(self, payload: dict | None = None, **kwargs) -> dict:
    """Reliably delete object-storage keys after the DB marked rows deleting.

    Flow (item 13):
      DB marks deleting + writes resource.delete OutboxEvent
      → dispatcher publishes this task
      → storage delete succeeds
      → hard-delete DB row / mark deleted

    Storage failures retry with exponential backoff. Exhausted retries go to DLQ.
    """
    payload = payload or kwargs.get("payload") or {}
    object_keys = list(payload.get("object_keys") or [])
    document_id = payload.get("document_id")
    kb_id = payload.get("kb_id")
    workspace_id = payload.get("workspace_id")
    max_retries = settings.resource_delete_max_retries

    storage = get_object_storage()
    try:
        for key in object_keys:
            if key:
                storage.delete(key)
    except Exception as exc:
        RESOURCE_DELETE_FAILURES.inc()
        retries = self.request.retries
        if retries < max_retries:
            countdown = min(300, 2 ** (retries + 1) * 5)
            raise self.retry(exc=exc, countdown=countdown)
        with SyncSessionLocal() as db:
            record_dead_letter(
                db,
                source="cleanup",
                task_name="delete_resources",
                error=str(exc),
                document_id=document_id,
                kb_id=kb_id,
                workspace_id=workspace_id,
                payload=payload,
                retry_count=retries,
            )
            db.commit()
        return {"ok": False, "reason": "dead_lettered"}

    with SyncSessionLocal() as db:
        if document_id:
            document = db.get(Document, document_id)
            if document is not None and document.status == DocStatus.deleting:
                # Soft tombstone then hard delete: status=deleted briefly for audit
                # visibility, then remove the row (cascades chunks). Object is gone.
                document.status = DocStatus.deleted
                document.stage = DocStatus.deleted.value
                document.finished_at = _now()
                db.flush()
                db.delete(document)
        db.commit()
    if kb_id:
        from app.services.semantic_cache import invalidate_kb_sync

        invalidate_kb_sync(kb_id)
    return {"ok": True, "deleted_keys": len(object_keys)}


@celery_app.task(name="purge_expired_audit_logs")
def purge_expired_audit_logs() -> dict:
    """Archive then delete audit rows past retention (item 17).

    Archive writes a JSONL blob to object storage before the append-only trigger
    allows deletion under app.audit_maintenance=on. Operators can re-verify the
    hash chain on the archive independently of the live table.
    """
    cutoff = _now() - timedelta(days=settings.audit_retention_days)
    archived = 0
    deleted = 0
    with SyncSessionLocal() as db:
        expired = list(
            db.execute(
                select(AuditLog)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.chain_seq.nulls_last(), AuditLog.created_at)
            ).scalars()
        )
        if not expired:
            return {"deleted": 0, "archived": 0, "retention_days": settings.audit_retention_days}

        if settings.audit_archive_before_purge:
            lines = []
            for row in expired:
                lines.append(
                    json.dumps(
                        {
                            "id": row.id,
                            "chain_seq": row.chain_seq,
                            "prev_hash": row.prev_hash,
                            "entry_hash": row.entry_hash,
                            "organization_id": row.organization_id,
                            "workspace_id": row.workspace_id,
                            "actor_user_id": row.actor_user_id,
                            "action": row.action,
                            "resource_type": row.resource_type,
                            "resource_id": row.resource_id,
                            "outcome": row.outcome,
                            "source_ip": row.source_ip,
                            "request_id": row.request_id,
                            "trace_id": row.trace_id,
                            "before": row.before,
                            "after": row.after,
                            "created_at": row.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
            archive_key = (
                f"audit-archives/{cutoff.date().isoformat()}/{uuid.uuid4().hex}.jsonl"
            )
            staging = make_staging_file(".jsonl")
            try:
                Path(staging).write_text("\n".join(lines) + "\n", encoding="utf-8")
                get_object_storage().put_file(archive_key, staging)
                archived = len(lines)
            except Exception:
                logger.exception("audit archive upload failed; refusing to purge")
                Path(staging).unlink(missing_ok=True)
                return {
                    "deleted": 0,
                    "archived": 0,
                    "error": "archive_failed",
                    "retention_days": settings.audit_retention_days,
                }

        db.execute(text("SELECT set_config('app.audit_maintenance', 'on', true)"))
        result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        deleted = result.rowcount or 0
        db.commit()
    return {
        "deleted": deleted,
        "archived": archived,
        "retention_days": settings.audit_retention_days,
    }


@celery_app.task(name="retry_pending_dead_letters")
def retry_pending_dead_letters(
    cooldown_hours: int = 1,
    max_per_run: int = 20,
) -> dict:
    """Periodically re-enqueue pending dead letters after a cooldown (item 12).

    Only retries ingest/cleanup dead letters whose original failure may have been
    transient. Dead letters that have already been replayed/discarded are skipped.
    The cooldown ensures we don't retry in a tight loop — operators should
    investigate dead letters that keep re-appearing.
    """
    cutoff = _now() - timedelta(hours=cooldown_hours)
    retried = 0
    with SyncSessionLocal() as db:
        pending = list(
            db.execute(
                select(DeadLetterTask)
                .where(
                    DeadLetterTask.status == DeadLetterStatus.pending,
                    DeadLetterTask.source.in_(["ingest", "cleanup"]),
                    DeadLetterTask.created_at < cutoff,
                )
                .order_by(DeadLetterTask.created_at)
                .limit(max_per_run)
            ).scalars()
        )
        for letter in pending:
            event_id = uuid.uuid4().hex
            if letter.source == "ingest" and letter.document_id:
                document = db.get(Document, letter.document_id)
                if document is None:
                    letter.status = DeadLetterStatus.discarded
                    letter.resolved_at = _now()
                    letter.error = (letter.error or "") + " | auto-discarded: document gone"
                    continue
                if document.status not in {DocStatus.failed, DocStatus.retrying}:
                    letter.status = DeadLetterStatus.discarded
                    letter.resolved_at = _now()
                    letter.error = (
                        letter.error or ""
                    ) + f" | auto-discarded: document is {document.status.value}"
                    continue
                document.status = DocStatus.queued
                document.stage = DocStatus.queued.value
                document.error = None
                document.processing_token = None
                document.worker_id = None
                if document.target_index_version is None:
                    document.target_index_version = (document.active_index_version or 0) + 1
                db.add(
                    OutboxEvent(
                        id=event_id,
                        event_type="document.ingest.requested",
                        aggregate_type="document",
                        aggregate_id=document.id,
                        payload={
                            "document_id": document.id,
                            "workspace_id": letter.workspace_id,
                            "reason": "dlq-auto-retry",
                            "from_stage": letter.failed_stage or "parsing",
                        },
                        dedup_key=f"document.ingest.dlq-auto:{letter.id}:{event_id}",
                    )
                )
            elif letter.source == "cleanup":
                db.add(
                    OutboxEvent(
                        id=event_id,
                        event_type="resource.delete.requested",
                        aggregate_type="document",
                        aggregate_id=letter.document_id or letter.kb_id or letter.id,
                        payload=dict(letter.payload or {}),
                        dedup_key=f"resource.delete.dlq-auto:{letter.id}:{event_id}",
                    )
                )
            else:
                continue
            letter.status = DeadLetterStatus.replayed
            letter.resolved_at = _now()
            retried += 1
        db.commit()
    logger.info("dlq auto-retry replayed %d dead letters", retried)
    return {"retried": retried, "cooldown_hours": cooldown_hours}
