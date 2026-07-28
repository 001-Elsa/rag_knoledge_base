"""Refresh-token families with atomic rotation and replay detection."""

import json
import uuid

from app.config import settings
from app.security import create_access_token, create_refresh_token, decode_token
from app.services.history import get_redis

_PREFIX = "auth:refresh:"
_FAMILY_PREFIX = "auth:family:"
_REVOKED_PREFIX = "auth:family-revoked:"
_FAMILY_USER_PREFIX = "auth:family-user:"
_USER_FAMILIES_PREFIX = "auth:user-families:"
_WS_TICKET_PREFIX = "auth:ws-ticket:"


async def issue_token_pair(
    user_id: str, family_id: str | None = None
) -> tuple[str, str]:
    family_id = family_id or uuid.uuid4().hex
    jti = uuid.uuid4().hex
    ttl = settings.refresh_token_expire_days * 86400
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.setex(
        _PREFIX + jti,
        ttl,
        json.dumps({"user_id": user_id, "family_id": family_id}),
    )
    pipe.sadd(_FAMILY_PREFIX + family_id, jti)
    pipe.expire(_FAMILY_PREFIX + family_id, ttl)
    pipe.setex(_FAMILY_USER_PREFIX + family_id, ttl, user_id)
    pipe.sadd(_USER_FAMILIES_PREFIX + user_id, family_id)
    pipe.expire(_USER_FAMILIES_PREFIX + user_id, ttl)
    await pipe.execute()
    return create_access_token(user_id), create_refresh_token(user_id, jti, family_id)


async def revoke_token_family(family_id: str) -> None:
    redis = get_redis()
    family_key = _FAMILY_PREFIX + family_id
    members = await redis.smembers(family_key)
    user_id = await redis.get(_FAMILY_USER_PREFIX + family_id)
    ttl = settings.refresh_token_expire_days * 86400
    pipe = redis.pipeline()
    for jti in members:
        pipe.delete(_PREFIX + jti)
    pipe.delete(family_key)
    pipe.delete(_FAMILY_USER_PREFIX + family_id)
    if user_id:
        pipe.srem(_USER_FAMILIES_PREFIX + user_id, family_id)
    pipe.setex(_REVOKED_PREFIX + family_id, ttl, "1")
    await pipe.execute()


async def revoke_all_user_families(user_id: str) -> None:
    redis = get_redis()
    family_ids = await redis.smembers(_USER_FAMILIES_PREFIX + user_id)
    for family_id in family_ids:
        await revoke_token_family(family_id)
    await redis.delete(_USER_FAMILIES_PREFIX + user_id)


async def rotate_refresh_token(refresh_token: str) -> tuple[str, str] | None:
    payload = decode_token(refresh_token, "refresh")
    if payload is None or not payload.get("family"):
        return None
    family_id = payload["family"]
    redis = get_redis()
    if await redis.exists(_REVOKED_PREFIX + family_id):
        return None

    stored = await redis.getdel(_PREFIX + payload["jti"])
    if stored is None:
        # A valid signed token whose one-time record disappeared is a replay signal.
        await revoke_token_family(family_id)
        return None
    try:
        record = json.loads(stored)
    except (TypeError, json.JSONDecodeError):
        await revoke_token_family(family_id)
        return None
    if (
        record.get("user_id") != payload["sub"]
        or record.get("family_id") != family_id
    ):
        await revoke_token_family(family_id)
        return None
    await redis.srem(_FAMILY_PREFIX + family_id, payload["jti"])
    return await issue_token_pair(payload["sub"], family_id=family_id)


async def revoke_refresh_token(refresh_token: str) -> None:
    payload = decode_token(refresh_token, "refresh")
    if payload is not None and payload.get("family"):
        await revoke_token_family(payload["family"])


async def issue_websocket_ticket(user_id: str) -> str:
    ticket = uuid.uuid4().hex
    await get_redis().setex(
        _WS_TICKET_PREFIX + ticket,
        settings.websocket_ticket_ttl_seconds,
        user_id,
    )
    return ticket


async def consume_websocket_ticket(ticket: str) -> str | None:
    if not ticket:
        return None
    return await get_redis().getdel(_WS_TICKET_PREFIX + ticket)
