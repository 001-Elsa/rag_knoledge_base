"""文档管理接口：上传 / 列表 / 状态 / 删除。"""
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models import Document, User
from app.routers.kb import get_owned_kb
from app.schemas import DocumentOut
from app.services import semantic_cache
from app.services.parser import SUPPORTED_EXTENSIONS
from app.tasks.ingest import ingest_document

router = APIRouter(prefix="/api/documents", tags=["文档"])


@router.post("", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.rate_limit_upload)
async def upload_document(
    request: Request,
    file: UploadFile,
    kb_id: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传文档到指定知识库。接口只做「存文件 + 建记录 + 投递任务」，立刻返回 202；
    解析/向量化由 Celery worker 异步完成，状态经 WebSocket 实时推送。"""
    await get_owned_kb(db, kb_id, user.id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的文件类型 {ext}，支持: {sorted(SUPPORTED_EXTENSIONS)}")

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{uuid.uuid4().hex}{ext}"

    # 流式落盘：分块读写，避免大文件一次性读进内存
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    async with aiofiles.open(saved_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                await out.close()
                saved_path.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"文件超过 {settings.max_upload_mb}MB 限制",
                )
            await out.write(chunk)

    doc = Document(
        owner_id=user.id, kb_id=kb_id, filename=file.filename or saved_path.name, filepath=str(saved_path)
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    ingest_document.delay(doc.id)  # 投递异步任务
    return doc


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    kb_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Document).where(Document.owner_id == user.id)
    if kb_id:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at.desc())
    return list((await db.execute(stmt)).scalars())


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    doc = await _owned_document(db, document_id, user.id)
    return doc


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    doc = await _owned_document(db, document_id, user.id)
    Path(doc.filepath).unlink(missing_ok=True)
    await db.delete(doc)  # chunks 级联删除
    await db.commit()
    await semantic_cache.invalidate_user(user.id)  # 内容变化，语义缓存失效


async def _owned_document(db: AsyncSession, document_id: str, owner_id: str) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None or doc.owner_id != owner_id:
        # 越权访问也返回 404 而不是 403，不暴露资源是否存在
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return doc
