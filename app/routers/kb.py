"""Knowledge-base management under workspace RBAC."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Document,
    KnowledgeBase,
    User,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas import KBCreateRequest, KBOut
from app.services.audit import add_audit_event
from app.services.permissions import get_kb_with_permission
from app.services.resource_cleanup import delete_knowledge_base

router = APIRouter(prefix="/api/kbs", tags=["知识库"])


@router.post("", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    body: KBCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    membership_stmt = select(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.role.in_(
            [WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.editor]
        ),
    )
    if body.workspace_id:
        membership_stmt = membership_stmt.where(
            WorkspaceMembership.workspace_id == body.workspace_id
        )
    membership = (await db.execute(membership_stmt.limit(1))).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工作区不存在")

    duplicate = (
        await db.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.workspace_id == membership.workspace_id,
                KnowledgeBase.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, "同名知识库已存在")

    kb = KnowledgeBase(
        owner_id=user.id,
        workspace_id=membership.workspace_id,
        name=body.name,
        description=body.description,
    )
    db.add(kb)
    await db.flush()
    add_audit_event(
        db,
        action="knowledge_base.create",
        resource_type="knowledge_base",
        resource_id=kb.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        after={"name": kb.name, "description": kb.description},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "同名知识库已存在") from None
    await db.refresh(kb)
    return KBOut(
        id=kb.id,
        workspace_id=kb.workspace_id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at,
        doc_count=0,
    )


@router.get("", response_model=list[KBOut])
async def list_kbs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(KnowledgeBase, func.count(Document.id))
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
        )
        .outerjoin(Document, Document.kb_id == KnowledgeBase.id)
        .where(WorkspaceMembership.user_id == user.id)
        .group_by(KnowledgeBase.id)
        .order_by(KnowledgeBase.created_at)
    )
    return [
        KBOut(
            id=kb.id,
            workspace_id=kb.workspace_id,
            name=kb.name,
            description=kb.description,
            created_at=kb.created_at,
            doc_count=count,
        )
        for kb, count in (await db.execute(stmt)).all()
    ]


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await get_kb_with_permission(db, kb_id, user.id, "delete")
    add_audit_event(
        db,
        action="knowledge_base.delete",
        resource_type="knowledge_base",
        resource_id=kb.id,
        actor_user_id=user.id,
        workspace_id=kb.workspace_id,
        request=request,
        before={"name": kb.name, "description": kb.description},
    )
    await delete_knowledge_base(db, kb, user.id)


async def get_owned_kb(
    db: AsyncSession, kb_id: str, owner_id: str
) -> KnowledgeBase:
    """Compatibility wrapper: callers now get workspace-level read access."""
    return await get_kb_with_permission(db, kb_id, owner_id, "read")
