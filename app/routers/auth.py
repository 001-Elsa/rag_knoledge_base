"""认证接口：注册 / 登录 / 刷新 / 登出 / 当前用户。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import LoginRequest, MeResponse, RefreshRequest, RegisterRequest, TokenPairResponse
from app.security import hash_password, verify_password
from app.services.tokens import issue_token_pair, revoke_refresh_token, rotate_refresh_token

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=TokenPairResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    await db.commit()
    access, refresh = await issue_token_pair(user.id)
    return TokenPairResponse(access_token=access, refresh_token=refresh)


@router.post("/login", response_model=TokenPairResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # 故意不区分"用户不存在"和"密码错误"，避免撞库攻击探测账号
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    access, refresh = await issue_token_pair(user.id)
    return TokenPairResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(body: RefreshRequest):
    """Refresh Token 轮换：旧 token 一经使用立即作废（防重放）。"""
    pair = await rotate_refresh_token(body.refresh_token)
    if pair is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh Token 无效或已过期，请重新登录")
    access, new_refresh = pair
    return TokenPairResponse(access_token=access, refresh_token=new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest):
    await revoke_refresh_token(body.refresh_token)


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)):
    return user
