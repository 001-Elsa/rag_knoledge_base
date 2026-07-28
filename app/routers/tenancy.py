"""Workspace membership and audit-log governance endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    AuditLog,
    Organization,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas import (
    AuditLogOut,
    MemberCreateRequest,
    MemberOut,
    MemberRoleUpdateRequest,
    OrganizationCreateRequest,
    OrganizationOut,
    WorkspaceCreateRequest,
    WorkspaceOut,
)
from app.services.audit import add_audit_event

router = APIRouter(prefix="/api/workspaces", tags=["租户与权限"])
organization_router = APIRouter(prefix="/api/organizations", tags=["租户与权限"])


async def _membership(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
    roles: set[WorkspaceRole],
) -> WorkspaceMembership:
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role.in_(roles),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工作区不存在")
    return membership


@organization_router.post(
    "", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED
)
async def create_organization(
    body: OrganizationCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    organization = Organization(name=body.name, created_by=user.id)
    db.add(organization)
    await db.flush()
    workspace = Workspace(organization_id=organization.id, name="Default")
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.owner,
        )
    )
    await db.flush()
    add_audit_event(
        db,
        action="organization.create",
        resource_type="organization",
        resource_id=organization.id,
        actor_user_id=user.id,
        organization_id=organization.id,
        workspace_id=workspace.id,
        request=request,
        after={"name": organization.name},
    )
    await db.commit()
    await db.refresh(organization)
    return organization


@organization_router.get("", response_model=list[OrganizationOut])
async def list_organizations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return list(
        (
            await db.execute(
                select(Organization)
                .join(Workspace, Workspace.organization_id == Organization.id)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.workspace_id == Workspace.id,
                )
                .where(WorkspaceMembership.user_id == user.id)
                .distinct()
                .order_by(Organization.created_at)
            )
        ).scalars()
    )


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    can_admin = (
        await db.execute(
            select(WorkspaceMembership.id)
            .join(
                Workspace,
                Workspace.id == WorkspaceMembership.workspace_id,
            )
            .where(
                Workspace.organization_id == body.organization_id,
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.role.in_(
                    [WorkspaceRole.owner, WorkspaceRole.admin]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if can_admin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    workspace = Workspace(
        organization_id=body.organization_id,
        name=body.name,
    )
    db.add(workspace)
    await db.flush()
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.owner,
    )
    db.add(membership)
    await db.flush()
    add_audit_event(
        db,
        action="workspace.create",
        resource_type="workspace",
        resource_id=workspace.id,
        actor_user_id=user.id,
        workspace_id=workspace.id,
        organization_id=body.organization_id,
        request=request,
        after={"name": workspace.name},
    )
    await db.commit()
    return WorkspaceOut(
        id=workspace.id,
        organization_id=workspace.organization_id,
        name=workspace.name,
        role=membership.role.value,
        created_at=workspace.created_at,
    )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Workspace, WorkspaceMembership.role)
            .join(
                WorkspaceMembership,
                WorkspaceMembership.workspace_id == Workspace.id,
            )
            .where(WorkspaceMembership.user_id == user.id)
            .order_by(Workspace.created_at)
        )
    ).all()
    return [
        WorkspaceOut(
            id=workspace.id,
            organization_id=workspace.organization_id,
            name=workspace.name,
            role=role.value,
            created_at=workspace.created_at,
        )
        for workspace, role in rows
    ]


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _membership(
        db,
        workspace_id,
        user.id,
        {WorkspaceRole.owner, WorkspaceRole.admin},
    )
    rows = (
        await db.execute(
            select(WorkspaceMembership, User.username)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(WorkspaceMembership.workspace_id == workspace_id)
            .order_by(WorkspaceMembership.created_at)
        )
    ).all()
    return [
        MemberOut(
            user_id=membership.user_id,
            username=username,
            role=membership.role.value,
            created_at=membership.created_at,
        )
        for membership, username in rows
    ]


@router.post(
    "/{workspace_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    workspace_id: str,
    body: MemberCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _membership(
        db,
        workspace_id,
        user.id,
        {WorkspaceRole.owner, WorkspaceRole.admin},
    )
    target = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    existing = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户已在工作区中")
    membership = WorkspaceMembership(
        workspace_id=workspace_id,
        user_id=target.id,
        role=WorkspaceRole(body.role),
    )
    db.add(membership)
    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    add_audit_event(
        db,
        action="workspace.member.add",
        resource_type="workspace_membership",
        resource_id=membership.id,
        actor_user_id=user.id,
        workspace_id=workspace_id,
        organization_id=workspace.organization_id,
        request=request,
        after={"user_id": target.id, "role": body.role, "actor_role": actor.role.value},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "用户已在工作区中") from None
    await db.refresh(membership)
    return MemberOut(
        user_id=target.id,
        username=target.username,
        role=membership.role.value,
        created_at=membership.created_at,
    )


@router.patch(
    "/{workspace_id}/members/{member_user_id}",
    response_model=MemberOut,
)
async def update_member_role(
    workspace_id: str,
    member_user_id: str,
    body: MemberRoleUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _membership(
        db,
        workspace_id,
        user.id,
        {WorkspaceRole.owner, WorkspaceRole.admin},
    )
    target = (
        await db.execute(
            select(WorkspaceMembership, User.username)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == member_user_id,
            )
        )
    ).one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    membership, username = target
    if membership.role == WorkspaceRole.owner:
        raise HTTPException(status.HTTP_409_CONFLICT, "Owner 角色不能通过该接口降级")
    if membership.role == WorkspaceRole.admin and actor.role != WorkspaceRole.owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    previous_role = membership.role.value
    membership.role = WorkspaceRole(body.role)
    workspace = await db.get(Workspace, workspace_id)
    add_audit_event(
        db,
        action="workspace.member.role.update",
        resource_type="workspace_membership",
        resource_id=membership.id,
        actor_user_id=user.id,
        workspace_id=workspace_id,
        organization_id=workspace.organization_id,
        request=request,
        before={"user_id": member_user_id, "role": previous_role},
        after={"user_id": member_user_id, "role": body.role},
    )
    await db.commit()
    return MemberOut(
        user_id=member_user_id,
        username=username,
        role=membership.role.value,
        created_at=membership.created_at,
    )


@router.delete(
    "/{workspace_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    workspace_id: str,
    member_user_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _membership(
        db,
        workspace_id,
        user.id,
        {WorkspaceRole.owner, WorkspaceRole.admin},
    )
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == member_user_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.role == WorkspaceRole.owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    if membership.role == WorkspaceRole.admin and actor.role != WorkspaceRole.owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    workspace = await db.get(Workspace, workspace_id)
    add_audit_event(
        db,
        action="workspace.member.remove",
        resource_type="workspace_membership",
        resource_id=membership.id,
        actor_user_id=user.id,
        workspace_id=workspace_id,
        organization_id=workspace.organization_id,
        request=request,
        before={"user_id": member_user_id, "role": membership.role.value},
    )
    await db.delete(membership)
    await db.commit()


@router.get("/{workspace_id}/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    workspace_id: str,
    limit: int = 100,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _membership(
        db,
        workspace_id,
        user.id,
        {WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.auditor},
    )
    limit = max(1, min(limit, 500))
    return list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.workspace_id == workspace_id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
        ).scalars()
    )
