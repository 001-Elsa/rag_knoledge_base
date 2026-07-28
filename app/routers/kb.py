"""知识库管理接口：创建 / 列表（含文档数）/ 删除。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Document, KnowledgeBase, User
from app.schemas import KBCreateRequest, KBOut
from app.services.resource_cleanup import delete_knowledge_base

router = APIRouter(prefix="/api/kbs", tags=["知识库"])


@router.post("", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_kb(body: KBCreateRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    dup = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.owner_id == user.id, KnowledgeBase.name == body.name)
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT, "同名知识库已存在")
    kb = KnowledgeBase(owner_id=user.id, name=body.name, description=body.description)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return KBOut(id=kb.id, name=kb.name, description=kb.description, created_at=kb.created_at, doc_count=0)


@router.get("", response_model=list[KBOut])
async def list_kbs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = (
        select(KnowledgeBase, func.count(Document.id))
        .outerjoin(Document, Document.kb_id == KnowledgeBase.id)
        .where(KnowledgeBase.owner_id == user.id)
        .group_by(KnowledgeBase.id)
        .order_by(KnowledgeBase.created_at)
    )
    rows = (await db.execute(stmt)).all()
    return [
        KBOut(id=kb.id, name=kb.name, description=kb.description, created_at=kb.created_at, doc_count=count)
        for kb, count in rows
    ]


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除知识库（级联删除其下文档与向量切片）。"""
    kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))).scalar_one_or_none()
    if kb is None or kb.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    await delete_knowledge_base(db, kb, user.id)


async def get_owned_kb(db: AsyncSession, kb_id: str, owner_id: str) -> KnowledgeBase:
    """校验知识库归属（供文档/对话接口复用）。"""
    kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))).scalar_one_or_none()
    if kb is None or kb.owner_id != owner_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    return kb
