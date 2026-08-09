"""Document upload, lifecycle, reindex, and deletion endpoints."""

import asyncio
import hashlib
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.metrics import TENANT_STORAGE_USAGE
from app.models import (
    Chunk,
    DocStatus,
    Document,
    KnowledgeBase,
    OutboxEvent,
    User,
    WorkspaceMembership,
)
from app.observability import set_trace_attributes
from app.schemas import DocumentMetadataUpdateRequest, DocumentOut
from app.services.audit import add_audit_event
from app.services.object_storage import get_object_storage, make_staging_file
from app.services.parser import SUPPORTED_EXTENSIONS
from app.services.permissions import (
    get_document_with_permission,
    get_kb_with_permission,
)
from app.services.resource_cleanup import delete_document as delete_document_resources
from app.services.tenancy import lock_workspace_quota

router = APIRouter(prefix="/api/documents", tags=["文档"])


def _validate_magic(extension: str, header: bytes) -> None:
    if extension == ".pdf" and not header.startswith(b"%PDF-"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件内容不是有效的 PDF")
    if extension in {".docx", ".xlsx", ".xlsm"} and not header.startswith(b"PK"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Office 文件内容不是有效的 ZIP 容器")
    image_signatures = {
        ".png": (b"\x89PNG",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".webp": (b"RIFF",),
        ".tif": (b"II*\x00", b"MM\x00*"),
        ".tiff": (b"II*\x00", b"MM\x00*"),
    }
    if extension in image_signatures and not any(
        header.startswith(signature) for signature in image_signatures[extension]
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片文件头无效")


def _trace_context() -> dict:
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier
    except ImportError:
        return {}


@router.post("", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.rate_limit_upload)
async def upload_document(
    request: Request,
    file: UploadFile,
    kb_id: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist object, Document, Outbox event, and audit event without DB/MQ dual writes."""
    kb = await get_kb_with_permission(db, kb_id, user.id, "write")
    extension = Path(file.filename or "").suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不支持的文件类型 {extension}，支持 {sorted(SUPPORTED_EXTENSIONS)}",
        )
    allowed_mime_types = {
        value.strip() for value in settings.allowed_mime_types.split(",") if value.strip()
    }
    if file.content_type and file.content_type not in allowed_mime_types:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件 MIME 类型不在允许列表中")

    staging_path = make_staging_file(extension)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    header = b""
    hasher = hashlib.sha256()
    try:
        async with aiofiles.open(staging_path, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                if not header:
                    header = chunk[:16]
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"文件超过 {settings.max_upload_mb}MB 限制",
                    )
                hasher.update(chunk)
                await output.write(chunk)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能上传空文件")
        _validate_magic(extension, header)

        await lock_workspace_quota(db, kb.workspace_id)
        used_bytes = (
            await db.execute(
                select(func.coalesce(func.sum(Document.size_bytes), 0))
                .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                .where(KnowledgeBase.workspace_id == kb.workspace_id)
            )
        ).scalar_one()
        quota_bytes = settings.workspace_storage_quota_mb * 1024 * 1024
        if used_bytes + written > quota_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "工作区存储配额不足",
            )

        content_hash = hasher.hexdigest()
        duplicate = (
            await db.execute(
                select(Document.id).where(
                    Document.owner_id == user.id,
                    Document.kb_id == kb_id,
                    Document.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "该文件已存在于当前知识库")

        object_key = (
            f"workspaces/{kb.workspace_id}/knowledge-bases/{kb.id}/"
            f"{uuid.uuid4().hex}{extension}"
        )
        storage = await asyncio.to_thread(get_object_storage)
        await asyncio.to_thread(storage.put_file, object_key, staging_path)

        document = Document(
            owner_id=user.id,
            kb_id=kb_id,
            filename=file.filename or f"document{extension}",
            filepath=object_key,
            object_key=object_key,
            content_hash=content_hash,
            mime_type=file.content_type,
            size_bytes=written,
            source_type="upload",
            source_metadata={"original_filename": file.filename or ""},
            status=DocStatus.uploaded,
            stage=DocStatus.uploaded.value,
        )
        db.add(document)
        await db.flush()
        set_trace_attributes(
            **{
                "tenant.workspace_id": kb.workspace_id,
                "rag.knowledge_base_id": kb.id,
                "rag.document_id": document.id,
            }
        )
        db.add(
            OutboxEvent(
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document.id,
                payload={
                    "document_id": document.id,
                    "workspace_id": kb.workspace_id,
                    "trace_context": _trace_context(),
                },
                dedup_key=f"document.ingest.initial:{document.id}",
            )
        )
        add_audit_event(
            db,
            action="document.upload",
            resource_type="document",
            resource_id=document.id,
            actor_user_id=user.id,
            workspace_id=kb.workspace_id,
            request=request,
            after={
                "filename": document.filename,
                "content_hash": content_hash,
                "size_bytes": written,
            },
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await asyncio.to_thread(storage.delete, object_key)
            raise HTTPException(
                status.HTTP_409_CONFLICT, "该文件已存在于当前知识库"
            ) from None
        except Exception:
            await db.rollback()
            await asyncio.to_thread(storage.delete, object_key)
            raise
        await db.refresh(document)
        TENANT_STORAGE_USAGE.labels(kb.workspace_id).set(used_bytes + written)
        return document
    finally:
        staging_path.unlink(missing_ok=True)


@router.put("/{document_id}/content", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.rate_limit_upload)
async def replace_document_content(
    document_id: str,
    request: Request,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically point a document at new content and enqueue a versioned reindex."""
    document = await get_document_with_permission(db, document_id, user.id, "write")
    if document.status in {DocStatus.deleting, DocStatus.deleted}:
        raise HTTPException(status.HTTP_409_CONFLICT, "删除中的文档不能更新")
    extension = Path(file.filename or "").suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的文件类型 {extension}")
    staging = make_staging_file(extension)
    maximum = settings.max_upload_mb * 1024 * 1024
    size = 0
    header = b""
    hasher = hashlib.sha256()
    try:
        async with aiofiles.open(staging, "wb") as output:
            while block := await file.read(1024 * 1024):
                if not header:
                    header = block[:16]
                size += len(block)
                if size > maximum:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "替换文件超过大小限制")
                hasher.update(block)
                await output.write(block)
        if not size:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能上传空文件")
        _validate_magic(extension, header)
        # Lock only after the upload body has been staged, avoiding a long-held DB
        # connection while still preventing replace/delete/reindex races.
        document = await get_document_with_permission(
            db, document_id, user.id, "write", lock=True
        )
        if document.status in {DocStatus.deleting, DocStatus.deleted}:
            raise HTTPException(status.HTTP_409_CONFLICT, "删除中的文档不能更新")
        content_hash = hasher.hexdigest()
        duplicate = (
            await db.execute(
                select(Document.id).where(
                    Document.owner_id == user.id,
                    Document.kb_id == document.kb_id,
                    Document.content_hash == content_hash,
                    Document.id != document.id,
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            raise HTTPException(status.HTTP_409_CONFLICT, "相同内容已存在于当前知识库")
        kb = await db.get(KnowledgeBase, document.kb_id)
        await lock_workspace_quota(db, kb.workspace_id)
        used = (
            await db.execute(
                select(func.coalesce(func.sum(Document.size_bytes), 0))
                .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                .where(KnowledgeBase.workspace_id == kb.workspace_id)
            )
        ).scalar_one()
        if used - document.size_bytes + size > settings.workspace_storage_quota_mb * 1024 * 1024:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "工作区存储配额不足")
        new_key = f"workspaces/{kb.workspace_id}/knowledge-bases/{kb.id}/{uuid.uuid4().hex}{extension}"
        storage = get_object_storage()
        await asyncio.to_thread(storage.put_file, new_key, staging)
        old_key = document.object_key
        before = {"filename": document.filename, "content_hash": document.content_hash, "object_key": old_key}
        document.filename = file.filename or document.filename
        document.object_key = new_key
        document.filepath = new_key
        document.content_hash = content_hash
        document.mime_type = file.content_type
        document.size_bytes = size
        document.source_type = "upload"
        document.source_url = None
        document.status = DocStatus.queued
        document.stage = DocStatus.queued.value
        document.error = None
        document.target_index_version = (document.active_index_version or 0) + 1
        event_id = uuid.uuid4().hex
        db.add(
            OutboxEvent(
                id=event_id,
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document.id,
                payload={"document_id": document.id, "workspace_id": kb.workspace_id, "reason": "content-update"},
                dedup_key=f"document.ingest.update:{document.id}:{event_id}",
            )
        )
        add_audit_event(
            db,
            action="document.content.update",
            resource_type="document",
            resource_id=document.id,
            actor_user_id=user.id,
            workspace_id=kb.workspace_id,
            request=request,
            before=before,
            after={"filename": document.filename, "content_hash": content_hash, "object_key": new_key},
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await asyncio.to_thread(storage.delete, new_key)
            raise
        if old_key and old_key != new_key:
            try:
                await asyncio.to_thread(storage.delete, old_key)
            except Exception:
                # The orphan reconciler removes old unreferenced objects later.
                pass
        await db.refresh(document)
        return document
    finally:
        staging.unlink(missing_ok=True)


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    kb_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
        )
        .where(WorkspaceMembership.user_id == user.id)
    )
    if kb_id:
        await get_kb_with_permission(db, kb_id, user.id, "read")
        stmt = stmt.where(Document.kb_id == kb_id)
    return list(
        (await db.execute(stmt.order_by(Document.created_at.desc()))).scalars().unique()
    )


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_document_with_permission(db, document_id, user.id, "read")


@router.patch("/{document_id}/metadata", response_model=DocumentOut)
async def update_document_metadata(
    document_id: str,
    body: DocumentMetadataUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await get_document_with_permission(
        db, document_id, user.id, "write", lock=True
    )
    before = {
        "department": document.department,
        "tags": document.tags,
        "source_url": document.source_url,
    }
    document.department = body.department
    document.tags = list(dict.fromkeys(tag.strip()[:64] for tag in body.tags if tag.strip()))
    if body.source_url is not None:
        document.source_url = body.source_url
        await db.execute(
            update(Chunk)
            .where(Chunk.document_id == document.id)
            .values(source_url=body.source_url)
        )
    kb = await db.get(KnowledgeBase, document.kb_id)
    add_audit_event(
        db,
        action="document.metadata.update",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before=before,
        after={"department": document.department, "tags": document.tags, "source_url": document.source_url},
    )
    await db.commit()
    await db.refresh(document)
    return document


@router.post("/{document_id}/reindex", response_model=DocumentOut, status_code=202)
async def reindex_document(
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await get_document_with_permission(
        db, document_id, user.id, "write", lock=True
    )
    if document.status not in {DocStatus.ready, DocStatus.failed}:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档当前状态不允许重建索引")
    kb = await db.get(KnowledgeBase, document.kb_id)
    previous_status = document.status.value
    document.status = DocStatus.queued
    document.stage = DocStatus.queued.value
    document.error = None
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
                "reason": "manual-reindex",
                "trace_context": _trace_context(),
            },
            dedup_key=f"document.ingest.reindex:{document.id}:{event_id}",
        )
    )
    add_audit_event(
        db,
        action="document.reindex",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"status": previous_status, "index_version": document.active_index_version},
        after={"target_index_version": document.target_index_version},
    )
    await db.commit()
    await db.refresh(document)
    return document


@router.post("/{document_id}/cancel", response_model=DocumentOut)
async def cancel_ingestion(
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await get_document_with_permission(
        db, document_id, user.id, "write", lock=True
    )
    cancellable = {
        DocStatus.uploaded,
        DocStatus.queued,
        DocStatus.parsing,
        DocStatus.chunking,
        DocStatus.embedding,
        DocStatus.indexing,
        DocStatus.retrying,
    }
    if document.status not in cancellable:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档当前状态不能取消")
    previous_status = document.status.value
    cancelled = (
        await db.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.status.in_(cancellable),
            )
            .values(
                status=DocStatus.cancelled,
                stage=DocStatus.cancelled.value,
                processing_token=None,
                worker_id=None,
                error="cancelled by user",
            )
            .returning(Document.id)
        )
    ).scalar_one_or_none()
    if cancelled is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档状态已变化")
    kb = await db.get(KnowledgeBase, document.kb_id)
    add_audit_event(
        db,
        action="document.ingestion.cancel",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"status": previous_status},
        after={"status": DocStatus.cancelled.value},
    )
    await db.commit()
    await db.refresh(document)
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await get_document_with_permission(
        db, document_id, user.id, "delete", lock=True
    )
    kb = await db.get(KnowledgeBase, document.kb_id)
    add_audit_event(
        db,
        action="document.delete",
        resource_type="document",
        resource_id=document.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"filename": document.filename, "content_hash": document.content_hash},
    )
    await delete_document_resources(db, document, user.id)
