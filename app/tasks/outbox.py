"""Transactional outbox dispatcher.

Database writes create events in the same transaction as business state. This dispatcher
publishes them with at-least-once semantics; consumers must therefore be idempotent.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import SyncSessionLocal
from app.metrics import OUTBOX_DISPATCH_FAILURES, OUTBOX_PENDING
from app.models import DocStatus, Document, OutboxEvent, OutboxStatus
from app.tasks import celery_app
from app.tasks.ingest import ingest_document

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


def _backoff(retry_count: int) -> int:
    return min(300, settings.outbox_base_retry_seconds * (2 ** max(0, retry_count - 1)))


@celery_app.task(name="dispatch_outbox_batch")
def dispatch_outbox_batch() -> dict:
    now = _now()
    with SyncSessionLocal() as db:
        events = list(
            db.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status.in_(
                        [OutboxStatus.pending, OutboxStatus.publishing]
                    ),
                    OutboxEvent.next_retry_at <= now,
                )
                .order_by(OutboxEvent.created_at)
                .limit(settings.outbox_batch_size)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        # A crashed dispatcher leaves "publishing" rows recoverable after next_retry_at.
        for event in events:
            event.status = OutboxStatus.publishing
            event.last_attempt_at = now
            event.next_retry_at = now + timedelta(seconds=60)
        db.commit()

    sent = 0
    failed = 0
    for event_id in [event.id for event in events]:
        with SyncSessionLocal() as db:
            event = db.get(OutboxEvent, event_id)
            if event is None or event.status != OutboxStatus.publishing:
                continue
            try:
                if event.event_type == "document.ingest.requested":
                    # Stable task_id prevents multiple broker messages from being mistaken for
                    # distinct work; the database CAS remains the source of truth.
                    ingest_document.apply_async(
                        args=[event.aggregate_id],
                        task_id=f"outbox-{event.id}",
                        headers=event.payload.get("trace_context", {}),
                    )
                    document = db.get(Document, event.aggregate_id)
                    if document and document.status == DocStatus.uploaded:
                        document.status = DocStatus.queued
                        document.stage = DocStatus.queued.value
                else:
                    raise ValueError(f"unsupported outbox event: {event.event_type}")
                event.status = OutboxStatus.sent
                event.sent_at = _now()
                event.last_error = None
                sent += 1
            except Exception as exc:
                event.retry_count += 1
                event.last_error = str(exc)[:2000]
                event.status = (
                    OutboxStatus.failed
                    if event.retry_count >= settings.outbox_max_retries
                    else OutboxStatus.pending
                )
                event.next_retry_at = _now() + timedelta(
                    seconds=_backoff(event.retry_count)
                )
                failed += 1
                OUTBOX_DISPATCH_FAILURES.inc()
                logger.warning("outbox dispatch failed: %s", event.id, exc_info=True)
            db.commit()

    with SyncSessionLocal() as db:
        pending = len(
            list(
                db.execute(
                    select(OutboxEvent.id).where(
                        OutboxEvent.status.in_(
                            [OutboxStatus.pending, OutboxStatus.publishing]
                        )
                    )
                ).scalars()
            )
        )
        OUTBOX_PENDING.set(pending)
    return {"sent": sent, "failed": failed, "pending": pending}
