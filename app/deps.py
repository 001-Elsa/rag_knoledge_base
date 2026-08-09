"""FastAPI 公共依赖。"""
import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_tenant_context
from app.models import APIKeyCredential, User
from app.observability import set_trace_attributes
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    api_key: str | None = Depends(api_key_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = decode_access_token(credentials.credentials) if credentials else None
    if user_id is None and api_key:
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        credential = (
            await db.execute(
                select(APIKeyCredential).where(
                    APIKeyCredential.secret_hash == digest,
                    APIKeyCredential.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)  # noqa: UP017
        if credential and (credential.expires_at is None or credential.expires_at > now):
            user_id = credential.owner_id
            await db.execute(
                update(APIKeyCredential)
                .where(APIKeyCredential.id == credential.id)
                .values(last_used_at=now)
            )
            await db.commit()
    if user_id is None:
        detail = "未提供认证信息" if credentials is None and not api_key else "认证信息无效或已过期"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    # SET LOCAL is automatically restored after every commit by the session hook.
    await set_tenant_context(db, user.id)
    set_trace_attributes(**{"enduser.id": user.id})
    return user
