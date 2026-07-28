"""密码哈希与 JWT 签发/校验（双 Token 体系）。

- Access Token：短效（默认 30 分钟）、无状态，每个请求携带；
- Refresh Token：长效（默认 7 天）、带 jti，白名单存 Redis（见 services/tokens.py），
  支持轮换与主动吊销——回答"JWT 怎么做登出"这个经典问题。
"""
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(
    user_id: str,
    token_type: str,
    lifetime: timedelta,
    jti: str | None = None,
    family_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)  # noqa: UP017 - keep Python 3.10 local compatibility
    payload = {
        "sub": user_id,
        "type": token_type,
        "jti": jti or uuid.uuid4().hex,
        "iat": now,
        "exp": now + lifetime,
    }
    if family_id:
        payload["family"] = family_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str) -> str:
    return _create_token(user_id, TOKEN_TYPE_ACCESS, timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(
    user_id: str, jti: str, family_id: str = "legacy"
) -> str:
    return _create_token(
        user_id,
        TOKEN_TYPE_REFRESH,
        timedelta(days=settings.refresh_token_expire_days),
        jti=jti,
        family_id=family_id,
    )


def decode_token(token: str, expected_type: str) -> dict | None:
    """校验签名、有效期与 token 类型（防止拿 Refresh Token 当 Access Token 用）。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type or not payload.get("sub"):
        return None
    return payload


def decode_access_token(token: str) -> str | None:
    """返回 user_id；无效/过期/类型不符返回 None。"""
    payload = decode_token(token, TOKEN_TYPE_ACCESS)
    return payload["sub"] if payload else None
