"""Idempotent, versioned document-ingestion state machine."""

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone

import jieba
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, func, update

from app.config import settings
from app.db import SyncSessionLocal
from app.metrics import (
    INGESTION_COMPLETED,
    INGESTION_FAILURES,
    INGESTION_QUEUE_DELAY,
    INGESTION_RETRIES,
    INGESTION_STAGE_DURATION,
)
from app.models import Chunk, DocStatus, Document
from app.observability import set_trace_attributes
from app.services.chunker import split_text
from app.services.embedder import embed_documents
from app.services.notify import document_event, publish_sync
from app.services.object_storage import get_object_storage
from app.services.parser import parse_file
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
    value = f"recursive:{settings.chunk_size}:{settings.chunk_overlap}"
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
            document_event(document.id, DocStatus.parsing.value, filename=document.filename),
        )

        try:
            stage_start = time.perf_counter()
            with get_object_storage().materialize(document.object_key) as source_path:
                segments = parse_file(str(source_path))
            INGESTION_STAGE_DURATION.labels("parsing").observe(
                time.perf_counter() - stage_start
            )
            if not any(text.strip() for text, _ in segments):
                raise NonRetryableIngestionError(
                    "document parser returned no text; scanned PDFs require OCR"
                )

            document = _set_stage(db, document_id, token, DocStatus.chunking)
            publish_sync(
                document.owner_id,
                document_event(document.id, DocStatus.chunking.value, filename=document.filename),
            )
            stage_start = time.perf_counter()
            pieces: list[tuple[str, int | None, int, str]] = []
            for parent_seq, (text, page) in enumerate(segments):
                for piece in split_text(
                    text, settings.chunk_size, settings.chunk_overlap
                ):
                    pieces.append((piece, page, parent_seq, text))
            INGESTION_STAGE_DURATION.labels("chunking").observe(
                time.perf_counter() - stage_start
            )
            if not pieces:
                raise NonRetryableIngestionError("chunker returned no content")

            document = _set_stage(db, document_id, token, DocStatus.embedding)
            publish_sync(
                document.owner_id,
                document_event(document.id, DocStatus.embedding.value, filename=document.filename),
            )
            stage_start = time.perf_counter()
            vectors = embed_documents([piece for piece, _, _, _ in pieces])
            if len(vectors) != len(pieces):
                raise RuntimeError("embedding provider returned an unexpected vector count")
            INGESTION_STAGE_DURATION.labels("embedding").observe(
                time.perf_counter() - stage_start
            )

            document = _set_stage(db, document_id, token, DocStatus.indexing)
            publish_sync(
                document.owner_id,
                document_event(document.id, DocStatus.indexing.value, filename=document.filename),
            )
            stage_start = time.perf_counter()
            target_version = document.target_index_version
            if target_version is None:
                raise RuntimeError("target index version is missing")

            # A retry can safely rebuild only its target version. The active version remains
            # queryable until all new chunks and metadata commit atomically.
            db.execute(
                delete(Chunk).where(
                    Chunk.document_id == document.id,
                    Chunk.index_version == target_version,
                )
            )
            db.add_all(
                Chunk(
                    document_id=document.id,
                    owner_id=document.owner_id,
                    kb_id=document.kb_id,
                    index_version=target_version,
                    seq=index,
                    page=page,
                    parent_seq=parent_seq,
                    content=piece,
                    parent_content=parent_content,
                    content_tokens=func.to_tsvector("simple", _tokenize(piece)),
                    embedding=vector,
                )
                for index, (
                    (piece, page, parent_seq, parent_content),
                    vector,
                ) in enumerate(zip(pieces, vectors))
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
                    chunk_strategy="recursive",
                    chunk_config_hash=_chunk_config_hash(),
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

            from app.services.semantic_cache import invalidate_kb_sync

            invalidate_kb_sync(document.kb_id)
            publish_sync(
                document.owner_id,
                document_event(
                    document.id,
                    DocStatus.ready.value,
                    filename=document.filename,
                    chunk_count=len(pieces),
                    index_version=target_version,
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
            db.commit()
            document = db.get(Document, document_id)
            INGESTION_FAILURES.labels(str(retryable).lower()).inc()
            if document is not None:
                publish_sync(
                    document.owner_id,
                    document_event(
                        document.id,
                        new_status.value,
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
