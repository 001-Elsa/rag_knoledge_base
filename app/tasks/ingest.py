"""Idempotent, versioned, checkpointed document-ingestion state machine.

Reliability model:
- Database CAS on (status, processing_token) claims the document; any concurrent
  cancel/delete/recovery invalidates the lease and the worker backs off.
- Embedding runs in batches. Each batch commits its chunks for the target index
  version and refreshes the lease heartbeat in the same transaction, so:
  * the stale-worker reconciler never reclaims a live worker during a long
    embedding stage, and
  * a retry after a crash reuses already-embedded chunks (checkpoint resume)
    instead of recomputing everything, as long as the pipeline fingerprint
    (content hash + chunk config + embedding model) still matches.
- Terminal failures are parked in the dead_letter_tasks table for admin replay.
"""

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone

import jieba
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, func, select, update

from app.config import settings
from app.db import SyncSessionLocal
from app.metrics import (
    DEAD_LETTER_TOTAL,
    INGESTION_COMPLETED,
    INGESTION_FAILURES,
    INGESTION_QUEUE_DELAY,
    INGESTION_RETRIES,
    INGESTION_STAGE_DURATION,
    PROMPT_INJECTION_QUARANTINED,
)
from app.models import Chunk, DeadLetterTask, DocStatus, Document, KnowledgeBase
from app.observability import set_trace_attributes
from app.services.chunker import split_segments
from app.services.embedder import embed_documents
from app.services.evidence import should_quarantine
from app.services.graph import rebuild_document_graph
from app.services.notify import document_event, publish_sync
from app.services.object_storage import get_object_storage
from app.services.parser import parse_document
from app.tasks import celery_app

logger = logging.getLogger(__name__)


class NonRetryableIngestionError(ValueError):
    """The input is invalid and retrying cannot make it valid."""


class LeaseLostError(RuntimeError):
    """Another transition (cancel/delete/recovery) invalidated this worker lease."""


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


def _tokenize(text: str) -> str:
    return " ".join(token for token in jieba.cut_for_search(text) if token.strip())


def _chunk_config_hash() -> str:
    value = (
        f"{settings.chunk_strategy}:{settings.chunk_size}:{settings.chunk_overlap}:"
        f"{settings.semantic_chunk_threshold}"
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _pipeline_fingerprint(content_hash: str) -> str:
    value = f"{content_hash}:{_chunk_config_hash()}:{settings.embedding_model}"
    return hashlib.sha256(value.encode()).hexdigest()


def _set_stage(db, document_id: str, token: str, status: DocStatus) -> Document:
    now = _now()
    claimed = db.execute(
        update(Document)
        .where(Document.id == document_id, Document.processing_token == token)
        .values(status=status, stage=status.value, heartbeat_at=now, updated_at=now)
        .returning(Document.id)
    ).scalar_one_or_none()
    if claimed is None:
        raise LeaseLostError("ingestion lease was lost")
    db.commit()
    return db.get(Document, document_id)


def _touch_heartbeat(db, document_id: str, token: str) -> None:
    """Refresh the lease inside the caller's transaction (no commit here)."""
    refreshed = db.execute(
        update(Document)
        .where(Document.id == document_id, Document.processing_token == token)
        .values(heartbeat_at=_now())
        .returning(Document.id)
    ).scalar_one_or_none()
    if refreshed is None:
        raise LeaseLostError("ingestion lease was lost during embedding")


def record_dead_letter(
    db,
    *,
    source: str,
    task_name: str,
    error: str,
    document_id: str | None = None,
    kb_id: str | None = None,
    workspace_id: str | None = None,
    payload: dict | None = None,
    failed_stage: str | None = None,
    retry_count: int = 0,
) -> DeadLetterTask:
    if workspace_id is None and kb_id is not None:
        workspace_id = db.execute(
            select(KnowledgeBase.workspace_id).where(KnowledgeBase.id == kb_id)
        ).scalar_one_or_none()
    entry = DeadLetterTask(
        source=source,
        task_name=task_name,
        document_id=document_id,
        kb_id=kb_id,
        workspace_id=workspace_id,
        payload=payload or {},
        error=error[:2000],
        failed_stage=failed_stage,
        retry_count=retry_count,
    )
    db.add(entry)
    DEAD_LETTER_TOTAL.labels(source).inc()
    return entry


@celery_app.task(
    name="ingest_document",
    bind=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def ingest_document(self, document_id: str) -> dict:
    token = uuid.uuid4().hex
    now = _now()
    with SyncSessionLocal() as db:
        claimed_id = db.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_(
                    [DocStatus.uploaded, DocStatus.queued, DocStatus.retrying]
                ),
            )
            .values(
                status=DocStatus.parsing,
                stage=DocStatus.parsing.value,
                processing_token=token,
                worker_id=self.request.hostname,
                started_at=func.coalesce(Document.started_at, now),
                heartbeat_at=now,
                finished_at=None,
                error=None,
                target_index_version=func.coalesce(
                    Document.target_index_version,
                    func.coalesce(Document.active_index_version, 0) + 1,
                ),
                updated_at=now,
            )
            .returning(Document.id)
        ).scalar_one_or_none()
        db.commit()
        if claimed_id is None:
            document = db.get(Document, document_id)
            reason = "not_found" if document is None else f"status:{document.status.value}"
            logger.info("ingestion skipped document=%s reason=%s", document_id, reason)
            return {"ok": False, "reason": reason}

        document = db.get(Document, document_id)
        set_trace_attributes(
            **{
                "rag.document_id": document.id,
                "rag.knowledge_base_id": document.kb_id,
                "celery.task_id": self.request.id,
                "celery.worker_id": self.request.hostname,
            }
        )
        INGESTION_QUEUE_DELAY.observe(
            max(0.0, (now - document.created_at).total_seconds())
        )
        publish_sync(
            document.owner_id,
            document_event(
                document.id,
                DocStatus.parsing.value,
                kb_id=document.kb_id,
                filename=document.filename,
            ),
        )

        # Checkpoint resume is only safe while content, chunk config, and embedding
        # model are unchanged. A fingerprint mismatch discards previous progress.
        fingerprint = _pipeline_fingerprint(document.content_hash)
        reuse_checkpoints = document.pipeline_fingerprint == fingerprint
        if not reuse_checkpoints:
            db.execute(
                update(Document)
                .where(Document.id == document_id, Document.processing_token == token)
                .values(pipeline_fingerprint=fingerprint, updated_at=_now())
            )
            db.commit()

        current_stage = DocStatus.parsing.value
        try:
            stage_start = time.perf_counter()
            with get_object_storage().materialize(document.object_key) as source_path:
                segments = parse_document(
                    str(source_path), source_url=document.source_url
                )
            INGESTION_STAGE_DURATION.labels("parsing").observe(
                time.perf_counter() - stage_start
            )
            if not any(segment.text.strip() for segment in segments):
                raise NonRetryableIngestionError(
                    "document parser returned no text"
                )

            document = _set_stage(db, document_id, token, DocStatus.chunking)
            current_stage = DocStatus.chunking.value
            publish_sync(
                document.owner_id,
                document_event(
                    document.id,
                    DocStatus.chunking.value,
                    kb_id=document.kb_id,
                    filename=document.filename,
                ),
            )
            stage_start = time.perf_counter()
            pieces = split_segments(
                segments,
                strategy=settings.chunk_strategy,
                chunk_size=settings.chunk_size,
                overlap=settings.chunk_overlap,
                semantic_threshold=settings.semantic_chunk_threshold,
            )
            INGESTION_STAGE_DURATION.labels("chunking").observe(
                time.perf_counter() - stage_start
            )
            if not pieces:
                raise NonRetryableIngestionError("chunker returned no content")

            # Indirect prompt-injection review: quarantine keeps the document out of
            # retrieval until an admin releases it, without blocking ingestion.
            quarantine = should_quarantine([piece.content for piece in pieces])

            document = _set_stage(db, document_id, token, DocStatus.embedding)
            current_stage = DocStatus.embedding.value
            publish_sync(
                document.owner_id,
                document_event(
                    document.id,
                    DocStatus.embedding.value,
                    kb_id=document.kb_id,
                    filename=document.filename,
                ),
            )
            stage_start = time.perf_counter()
            target_version = document.target_index_version
            if target_version is None:
                raise RuntimeError("target index version is missing")
            kb = db.get(KnowledgeBase, document.kb_id)
            if kb is None:
                raise NonRetryableIngestionError("knowledge base not found")

            # A retry may reuse chunks that a previous attempt already embedded and
            # committed for the same target version (checkpoint resume). Anything
            # that does not match the freshly computed pieces is discarded.
            reusable: set[int] = set()
            if reuse_checkpoints:
                existing = dict(
                    db.execute(
                        select(Chunk.seq, Chunk.content).where(
                            Chunk.document_id == document.id,
                            Chunk.index_version == target_version,
                        )
                    ).all()
                )
                reusable = {
                    seq
                    for seq, piece in enumerate(pieces)
                    if existing.get(seq) == piece.content
                }
            stale_filter = [
                Chunk.document_id == document.id,
                Chunk.index_version == target_version,
            ]
            if reusable:
                stale_filter.append(Chunk.seq.notin_(reusable))
            db.execute(delete(Chunk).where(*stale_filter))
            db.commit()
            if reusable:
                logger.info(
                    "ingestion resume document=%s version=%d reused=%d/%d",
                    document_id,
                    target_version,
                    len(reusable),
                    len(pieces),
                )

            pending = [
                (seq, piece)
                for seq, piece in enumerate(pieces)
                if seq not in reusable
            ]
            batch_size = max(1, settings.ingestion_embed_batch_size)
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                vectors = embed_documents([piece.content for _, piece in batch])
                if len(vectors) != len(batch):
                    raise RuntimeError(
                        "embedding provider returned an unexpected vector count"
                    )
                db.add_all(
                    Chunk(
                        document_id=document.id,
                        owner_id=document.owner_id,
                        kb_id=document.kb_id,
                        workspace_id=kb.workspace_id,
                        index_version=target_version,
                        seq=seq,
                        page=piece.page,
                        parent_seq=piece.parent_seq,
                        section=piece.section,
                        content_type=piece.content_type,
                        source_url=piece.source_url,
                        content=piece.content,
                        parent_content=piece.parent_content,
                        token_count=len(_tokenize(piece.content).split()),
                        metadata_json={
                            "section": piece.section,
                            "page": piece.page,
                            "content_type": piece.content_type,
                            "department": document.department,
                            "tags": document.tags,
                        },
                        content_tokens=func.to_tsvector("simple", _tokenize(piece.content)),
                        embedding=vector,
                    )
                    for (seq, piece), vector in zip(batch, vectors)
                )
                # Chunk checkpoint and lease heartbeat commit atomically: a reclaimed
                # lease rolls the batch back and stops this worker.
                _touch_heartbeat(db, document_id, token)
                db.commit()
            INGESTION_STAGE_DURATION.labels("embedding").observe(
                time.perf_counter() - stage_start
            )

            document = _set_stage(db, document_id, token, DocStatus.indexing)
            current_stage = DocStatus.indexing.value
            publish_sync(
                document.owner_id,
                document_event(
                    document.id,
                    DocStatus.indexing.value,
                    kb_id=document.kb_id,
                    filename=document.filename,
                ),
            )
            stage_start = time.perf_counter()
            persisted = db.execute(
                select(func.count())
                .select_from(Chunk)
                .where(
                    Chunk.document_id == document.id,
                    Chunk.index_version == target_version,
                )
            ).scalar_one()
            if persisted != len(pieces):
                raise RuntimeError(
                    f"chunk count mismatch before activation: {persisted} != {len(pieces)}"
                )
            active_chunks = list(
                db.execute(
                    select(Chunk).where(
                        Chunk.document_id == document.id,
                        Chunk.index_version == target_version,
                    )
                ).scalars()
            )
            if settings.graph_retrieval_enabled:
                rebuild_document_graph(
                    db,
                    document=document,
                    chunks=active_chunks,
                    workspace_id=kb.workspace_id,
                )
            switched = db.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.processing_token == token,
                    Document.status == DocStatus.indexing,
                )
                .values(
                    status=DocStatus.ready,
                    stage=DocStatus.ready.value,
                    active_index_version=target_version,
                    target_index_version=None,
                    embedding_model=settings.embedding_model,
                    embedding_dim=settings.embedding_dim,
                    chunk_strategy=settings.chunk_strategy,
                    chunk_config_hash=_chunk_config_hash(),
                    pipeline_fingerprint=None,
                    quarantined=quarantine,
                    chunk_count=len(pieces),
                    error=None,
                    processing_token=None,
                    heartbeat_at=_now(),
                    finished_at=_now(),
                    updated_at=_now(),
                )
                .returning(Document.id)
            ).scalar_one_or_none()
            if switched is None:
                raise LeaseLostError("index activation CAS failed")
            db.commit()
            INGESTION_STAGE_DURATION.labels("indexing").observe(
                time.perf_counter() - stage_start
            )
            if quarantine:
                PROMPT_INJECTION_QUARANTINED.inc()
                logger.warning(
                    "document quarantined for prompt-injection review document=%s",
                    document_id,
                )

            from app.services.semantic_cache import invalidate_kb_sync

            invalidate_kb_sync(document.kb_id)
            publish_sync(
                document.owner_id,
                document_event(
                    document.id,
                    DocStatus.ready.value,
                    kb_id=document.kb_id,
                    filename=document.filename,
                    chunk_count=len(pieces),
                    index_version=target_version,
                    quarantined=quarantine,
                ),
            )
            logger.info(
                "ingestion complete document=%s chunks=%d version=%d",
                document_id,
                len(pieces),
                target_version,
            )
            INGESTION_COMPLETED.inc()
            return {
                "ok": True,
                "chunks": len(pieces),
                "index_version": target_version,
                "quarantined": quarantine,
            }

        except Exception as exc:
            db.rollback()
            if isinstance(exc, LeaseLostError):
                logger.info("ingestion lease lost document=%s", document_id)
                return {"ok": False, "reason": "lease_lost"}
            retryable = not isinstance(exc, NonRetryableIngestionError)
            retries_exhausted = self.request.retries >= self.max_retries
            new_status = (
                DocStatus.retrying
                if retryable and not retries_exhausted
                else DocStatus.failed
            )
            db.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.processing_token == token,
                )
                .values(
                    status=new_status,
                    stage=new_status.value,
                    error=str(exc)[:2000],
                    retry_count=Document.retry_count + 1,
                    processing_token=None,
                    worker_id=None,
                    heartbeat_at=_now(),
                    finished_at=_now() if new_status == DocStatus.failed else None,
                    updated_at=_now(),
                )
            )
            document = db.get(Document, document_id)
            if new_status == DocStatus.failed and document is not None:
                # Terminal failure: park in the dead letter queue for admin replay.
                record_dead_letter(
                    db,
                    source="ingest",
                    task_name="ingest_document",
                    error=str(exc),
                    document_id=document.id,
                    kb_id=document.kb_id,
                    payload={"document_id": document.id, "stage": current_stage},
                    failed_stage=current_stage,
                    retry_count=self.request.retries,
                )
            db.commit()
            document = db.get(Document, document_id)
            INGESTION_FAILURES.labels(str(retryable).lower()).inc()
            if document is not None:
                publish_sync(
                    document.owner_id,
                    document_event(
                        document.id,
                        new_status.value,
                        kb_id=document.kb_id,
                        filename=document.filename,
                        error=document.error,
                        retry_count=document.retry_count,
                    ),
                )
            logger.exception(
                "ingestion failed document=%s retryable=%s", document_id, retryable
            )
            if retryable and not retries_exhausted:
                INGESTION_RETRIES.inc()
                countdown = min(300, 2 ** (self.request.retries + 1) * 5)
                raise self.retry(exc=exc, countdown=countdown)
            if isinstance(exc, SoftTimeLimitExceeded):
                return {"ok": False, "reason": "soft_time_limit"}
            return {"ok": False, "reason": str(exc)[:500]}
