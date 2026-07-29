"""Admin APIs: dead letters, quarantine, stage resume, audit search/export."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.metrics import DEAD_LETTER_REPLAYED
from app.models import (
    AuditLog,
    DeadLetterStatus,
    DeadLetterTask,
    DocStatus,
    Document,
    KnowledgeBase,
    OutboxEvent,
    OutboxStatus,
    User,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas import (
    AuditLogOut,
    DeadLetterOut,
    DocumentOut,
    ReplayRequest,
    ResumeFromStageRequest,
)
from app.services.audit import add_audit_event

router = APIRouter(prefix="/api/admin", tags=["运维管理"])

_ADMIN_ROLES = {WorkspaceRole.owner, WorkspaceRole.admin}
_AUDIT_ROLES = {WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.auditor}

# Stages that may be resumed by re-queueing ingestion. Embedding checkpoints make
# resume from embedding/indexing cheap when the pipeline fingerprint still matches.
_RESUMABLE_STAGES = {
    "parsing",
    "chunking",
    "embedding",
    "indexing",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


async def _require_workspace_admin(
    db: AsyncSession, workspace_id: str, user_id: str
) -> WorkspaceMembership:
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role.in_(_ADMIN_ROLES),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工作区不存在")
    return membership


async def _require_workspace_auditor(
    db: AsyncSession, workspace_id: str, user_id: str
) -> WorkspaceMembership:
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role.in_(_AUDIT_ROLES),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工作区不存在")
    return membership


@router.get("/dead-letters", response_model=list[DeadLetterOut])
async def list_dead_letters(
    workspace_id: str = Query(...),
    status_filter: str | None = Query(default="pending", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_admin(db, workspace_id, user.id)
    stmt = (
        select(DeadLetterTask)
        .where(DeadLetterTask.workspace_id == workspace_id)
        .order_by(DeadLetterTask.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(DeadLetterTask.status == DeadLetterStatus(status_filter))
    return list((await db.execute(stmt)).scalars())


@router.post("/dead-letters/{letter_id}/replay", response_model=DeadLetterOut)
async def replay_dead_letter(
    letter_id: str,
    body: ReplayRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually re-enqueue a terminal failure (item 12)."""
    letter = await db.get(DeadLetterTask, letter_id)
    if letter is None or letter.workspace_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "死信任务不存在")
    await _require_workspace_admin(db, letter.workspace_id, user.id)
    if letter.status != DeadLetterStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "死信任务已处理")

    event_id = uuid.uuid4().hex
    if letter.source == "ingest" and letter.document_id:
        document = await db.get(Document, letter.document_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关联文档不存在")
        resume_stage = body.from_stage or letter.failed_stage or "parsing"
        if resume_stage not in _RESUMABLE_STAGES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不支持的续跑阶段")
        document.status = DocStatus.queued
        document.stage = DocStatus.queued.value
        document.error = None
        document.processing_token = None
        document.worker_id = None
        # Keep target_index_version / pipeline_fingerprint so embedding checkpoints
        # can be reused when content+config are unchanged.
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
                    "reason": "dead-letter-replay",
                    "from_stage": resume_stage,
                },
                dedup_key=f"document.ingest.dlq:{letter.id}:{event_id}",
            )
        )
    elif letter.source == "outbox":
        outbox_id = (letter.payload or {}).get("outbox_event_id")
        event = await db.get(OutboxEvent, outbox_id) if outbox_id else None
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关联 Outbox 事件不存在")
        event.status = OutboxStatus.pending
        event.next_retry_at = _now()
        event.last_error = None
    elif letter.source == "cleanup":
        db.add(
            OutboxEvent(
                id=event_id,
                event_type="resource.delete.requested",
                aggregate_type="document",
                aggregate_id=letter.document_id or letter.kb_id or letter.id,
                payload=dict(letter.payload or {}),
                dedup_key=f"resource.delete.dlq:{letter.id}:{event_id}",
            )
        )
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"无法重放来源: {letter.source}")

    letter.status = DeadLetterStatus.replayed
    letter.resolved_at = _now()
    letter.resolved_by = user.id
    add_audit_event(
        db,
        action="dead_letter.replay",
        resource_type="dead_letter_task",
        resource_id=letter.id,
        actor_user_id=user.id,
        workspace_id=letter.workspace_id,
        request=request,
        after={"source": letter.source, "from_stage": body.from_stage},
    )
    await db.commit()
    await db.refresh(letter)
    DEAD_LETTER_REPLAYED.inc()
    return letter


@router.post("/dead-letters/{letter_id}/discard", response_model=DeadLetterOut)
async def discard_dead_letter(
    letter_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    letter = await db.get(DeadLetterTask, letter_id)
    if letter is None or letter.workspace_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "死信任务不存在")
    await _require_workspace_admin(db, letter.workspace_id, user.id)
    if letter.status != DeadLetterStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "死信任务已处理")
    letter.status = DeadLetterStatus.discarded
    letter.resolved_at = _now()
    letter.resolved_by = user.id
    add_audit_event(
        db,
        action="dead_letter.discard",
        resource_type="dead_letter_task",
        resource_id=letter.id,
        actor_user_id=user.id,
        workspace_id=letter.workspace_id,
        request=request,
    )
    await db.commit()
    await db.refresh(letter)
    return letter


@router.post(
    "/documents/{document_id}/resume",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_from_stage(
    document_id: str,
    body: ResumeFromStageRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One-click resume from a named ingestion stage (item 12)."""
    if body.from_stage not in _RESUMABLE_STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不支持的续跑阶段")
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    kb = await db.get(KnowledgeBase, document.kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    await _require_workspace_admin(db, kb.workspace_id, user.id)
    if document.status not in {DocStatus.failed, DocStatus.retrying, DocStatus.cancelled}:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档当前状态不允许续跑")

    previous = document.status.value
    document.status = DocStatus.queued
    document.stage = DocStatus.queued.value
    document.error = None
    document.processing_token = None
    document.worker_id = None
    if document.target_index_version is None:
        document.target_index_version = (document.active_index_version or 0) + 1
    event_id = uuid.uuid4().hex
    db.add(
        OutboxEvent(
            id=event_id,
            event_type="document.ingest.requested",
            aggregate_type="document",
            aggregate_id=document.id,
            payload={
                "document_id": document.id,
                "workspace_id": kb.workspace_id,
                "reason": "admin-resume",
                "from_stage": body.from_stage,
            },
            dedup_key=f"document.ingest.resume:{document.id}:{event_id}",
        )
    )
    add_audit_event(
        db,
        action="document.ingestion.resume",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"status": previous},
        after={"from_stage": body.from_stage, "status": DocStatus.queued.value},
    )
    await db.commit()
    await db.refresh(document)
    return document


@router.get("/quarantine", response_model=list[DocumentOut])
async def list_quarantined(
    workspace_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_admin(db, workspace_id, user.id)
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            KnowledgeBase.workspace_id == workspace_id,
            Document.quarantined.is_(True),
        )
        .order_by(Document.updated_at.desc())
    )
    return list((await db.execute(stmt)).scalars())


@router.post("/quarantine/{document_id}/release", response_model=DocumentOut)
async def release_quarantine(
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    kb = await db.get(KnowledgeBase, document.kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    await _require_workspace_admin(db, kb.workspace_id, user.id)
    if not document.quarantined:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档不在隔离区")
    document.quarantined = False
    add_audit_event(
        db,
        action="document.quarantine.release",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"quarantined": True},
        after={"quarantined": False},
    )
    await db.commit()
    await db.refresh(document)
    from app.services.semantic_cache import invalidate_kb

    await invalidate_kb(document.kb_id)
    return document


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def search_audit_logs(
    workspace_id: str = Query(...),
    action: str | None = None,
    actor_user_id: str | None = None,
    resource_type: str | None = None,
    request_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_auditor(db, workspace_id, user.id)
    stmt = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if request_id:
        stmt = stmt.where(AuditLog.request_id == request_id)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars())


@router.get("/audit-logs/export")
async def export_audit_logs(
    workspace_id: str = Query(...),
    limit: int = Query(default=5000, ge=1, le=50000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """JSONL export for offline hash-chain verification (item 17)."""
    await _require_workspace_auditor(db, workspace_id, user.id)
    rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.workspace_id == workspace_id)
                .order_by(AuditLog.chain_seq.nulls_last(), AuditLog.created_at)
                .limit(limit)
            )
        ).scalars()
    )
    import json

    lines = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "id": row.id,
                    "chain_seq": row.chain_seq,
                    "prev_hash": row.prev_hash,
                    "entry_hash": row.entry_hash,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "actor_user_id": row.actor_user_id,
                    "workspace_id": row.workspace_id,
                    "outcome": row.outcome,
                    "request_id": row.request_id,
                    "trace_id": row.trace_id,
                    "before": row.before,
                    "after": row.after,
                    "created_at": row.created_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="audit-{workspace_id}.jsonl"'
        },
    )
