"""Authentication with in-memory access tokens and HttpOnly refresh cookies."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Organization,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.schemas import (
    LoginRequest,
    MeResponse,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    WebSocketTicketResponse,
)
from app.security import hash_password, verify_password
from app.services.audit import add_audit_event
from app.services.tokens import (
    issue_token_pair,
    issue_websocket_ticket,
    revoke_all_user_families,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path="/api/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.refresh_cookie_name,
        path="/api/auth",
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _body_refresh(token: str) -> str | None:
    return token if settings.expose_refresh_token_in_body else None


def _refresh_from_request(request: Request, body: RefreshRequest | None) -> str | None:
    return (
        body.refresh_token
        if body and body.refresh_token
        else request.cookies.get(settings.refresh_cookie_name)
    )


def _enforce_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "跨站刷新请求已拒绝")


@router.post("/register", response_model=TokenPairResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()
    organization = Organization(
        name=f"{body.username} organization", created_by=user.id
    )
    db.add(organization)
    await db.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Personal",
    )
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.owner,
        )
    )
    await db.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": user.id},
    )
    add_audit_event(
        db,
        action="identity.register",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=user.id,
        workspace_id=workspace.id,
        organization_id=organization.id,
        request=request,
        after={"username": user.username},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from None
    access, refresh = await issue_token_pair(user.id)
    _set_refresh_cookie(response, refresh)
    return TokenPairResponse(
        access_token=access, refresh_token=_body_refresh(refresh)
    )


@router.post("/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    access, refresh = await issue_token_pair(user.id)
    _set_refresh_cookie(response, refresh)
    return TokenPairResponse(
        access_token=access, refresh_token=_body_refresh(refresh)
    )


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
):
    _enforce_same_origin(request)
    old_refresh = _refresh_from_request(request, body)
    if not old_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Refresh Token")
    pair = await rotate_refresh_token(old_refresh)
    if pair is None:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Refresh Token 无效、已重放或已过期，请重新登录",
        )
    access, new_refresh = pair
    _set_refresh_cookie(response, new_refresh)
    return TokenPairResponse(
        access_token=access,
        refresh_token=_body_refresh(new_refresh),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
):
    _enforce_same_origin(request)
    refresh_token = _refresh_from_request(request, body)
    if refresh_token:
        await revoke_refresh_token(refresh_token)
    _clear_refresh_cookie(response)


@router.post("/ws-ticket", response_model=WebSocketTicketResponse)
async def websocket_ticket(user: User = Depends(get_current_user)):
    ticket = await issue_websocket_ticket(user.id)
    return WebSocketTicketResponse(
        ticket=ticket,
        expires_in=settings.websocket_ticket_ttl_seconds,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "当前密码错误")
    user.password_hash = hash_password(body.new_password)
    add_audit_event(
        db,
        action="identity.password.change",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=user.id,
        request=request,
    )
    await db.commit()
    await revoke_all_user_families(user.id)
    _clear_refresh_cookie(response)


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)):
    return user
