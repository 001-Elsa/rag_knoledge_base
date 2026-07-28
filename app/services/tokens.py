"""Refresh Token 白名单（Redis）：签发 / 轮换 / 吊销。

设计要点：
- Redis 只存 jti → user_id，key 自带 TTL 与 token 同寿命；
- 轮换用 GETDEL 原子取出并删除：旧 Refresh Token 一经使用立刻作废，
  被窃取的旧 token 无法二次使用（防重放）；
- 登出 = 删除 jti。Access Token 只有 30 分钟寿命，不做黑名单。
"""
import uuid

from app.config import settings
from app.security import create_access_token, create_refresh_token, decode_token
from app.services.history import get_redis

_PREFIX = "auth:refresh:"


async def issue_token_pair(user_id: str) -> tuple[str, str]:
    """签发 Access + Refresh，Refresh 的 jti 写入 Redis 白名单。"""
    jti = uuid.uuid4().hex
    ttl = settings.refresh_token_expire_days * 86400
    await get_redis().setex(_PREFIX + jti, ttl, user_id)
    return create_access_token(user_id), create_refresh_token(user_id, jti)


async def rotate_refresh_token(refresh_token: str) -> tuple[str, str] | None:
    """用 Refresh 换新 Token 对（轮换）。无效/已用过/已吊销返回 None。"""
    payload = decode_token(refresh_token, "refresh")
    if payload is None:
        return None
    stored_user = await get_redis().getdel(_PREFIX + payload["jti"])  # 原子：取出即作废
    if stored_user is None or stored_user != payload["sub"]:
        return None
    return await issue_token_pair(payload["sub"])


async def revoke_refresh_token(refresh_token: str) -> None:
    """登出：吊销 Refresh Token（幂等，无效 token 静默忽略）。"""
    payload = decode_token(refresh_token, "refresh")
    if payload is not None:
        await get_redis().delete(_PREFIX + payload["jti"])
