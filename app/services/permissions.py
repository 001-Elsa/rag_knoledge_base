"""Application-layer RBAC; PostgreSQL RLS provides a second isolation boundary."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    KnowledgeBase,
    WorkspaceMembership,
    WorkspaceRole,
)

READ_ROLES = {
    WorkspaceRole.owner,
    WorkspaceRole.admin,
    WorkspaceRole.editor,
    WorkspaceRole.viewer,
    WorkspaceRole.auditor,
}
QUERY_ROLES = {
    WorkspaceRole.owner,
    WorkspaceRole.admin,
    WorkspaceRole.editor,
    WorkspaceRole.viewer,
}
WRITE_ROLES = {WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.editor}
ADMIN_ROLES = {WorkspaceRole.owner, WorkspaceRole.admin}
AUDIT_ROLES = {WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.auditor}

PERMISSION_ROLES = {
    "read": READ_ROLES,
    "query": QUERY_ROLES,
    "write": WRITE_ROLES,
    "delete": ADMIN_ROLES,
    "admin": ADMIN_ROLES,
    "audit": AUDIT_ROLES,
}


async def get_kb_with_permission(
    db: AsyncSession,
    kb_id: str,
    user_id: str,
    permission: str = "read",
) -> KnowledgeBase:
    allowed = PERMISSION_ROLES[permission]
    stmt = (
        select(KnowledgeBase)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
        )
        .where(
            KnowledgeBase.id == kb_id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role.in_(allowed),
        )
    )
    kb = (await db.execute(stmt)).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    return kb


async def get_document_with_permission(
    db: AsyncSession,
    document_id: str,
    user_id: str,
    permission: str = "read",
    *,
    lock: bool = False,
) -> Document:
    allowed = PERMISSION_ROLES[permission]
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
        )
        .where(
            Document.id == document_id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role.in_(allowed),
        )
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    document = (await db.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return document
