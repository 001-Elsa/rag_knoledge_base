"""Distributed chat capacity leases with a process-local Redis outage fallback."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass

from app.config import settings
from app.services.history import get_redis

logger = logging.getLogger(__name__)

_ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local token = ARGV[3]
for i, key in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
  if redis.call('ZCARD', key) >= tonumber(ARGV[3 + i]) then
    return i
  end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, expires, token)
  redis.call('PEXPIRE', key, math.max(expires - now, 1000) * 2)
end
return 0
"""

_RENEW_SCRIPT = """
local token = ARGV[1]
local expires = tonumber(ARGV[2])
for _, key in ipairs(KEYS) do
  if not redis.call('ZSCORE', key, token) then return 0 end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, 'XX', expires, token)
  redis.call('PEXPIRE', key, tonumber(ARGV[3]))
end
return 1
"""

_RELEASE_SCRIPT = """
for _, key in ipairs(KEYS) do redis.call('ZREM', key, ARGV[1]) end
return 1
"""

_local_guard = threading.Lock()
_local_slots: dict[str, dict[str, float]] = {}


@dataclass(frozen=True)
class CapacityLease:
    token: str
    keys: tuple[str, ...]
    backend: str


def _keys_and_limits(user_id: str, conversation_id: str | None) -> tuple[tuple[str, ...], tuple[int, ...]]:
    # The shared hash tag keeps all keys in one Redis Cluster slot for atomic Lua.
    keys = ["rag:capacity:{chat}:global", f"rag:capacity:{{chat}}:user:{user_id}"]
    limits = [max(1, settings.chat_global_concurrency), max(1, settings.chat_user_concurrency)]
    if conversation_id:
        keys.append(f"rag:capacity:{{chat}}:conversation:{user_id}:{conversation_id}")
        limits.append(1)
    return tuple(keys), tuple(limits)


def _acquire_local(keys: tuple[str, ...], limits: tuple[int, ...], token: str) -> bool:
    now = time.monotonic()
    expires = now + max(1, settings.chat_slot_lease_seconds)
    with _local_guard:
        for key in keys:
            slots = _local_slots.setdefault(key, {})
            for stale in [item for item, deadline in slots.items() if deadline <= now]:
                slots.pop(stale, None)
        if any(len(_local_slots[key]) >= limit for key, limit in zip(keys, limits)):
            return False
        for key in keys:
            _local_slots[key][token] = expires
    return True


async def acquire_chat_slot(user_id: str, conversation_id: str | None = None) -> CapacityLease | None:
    keys, limits = _keys_and_limits(user_id, conversation_id)
    token = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + max(1, settings.chat_slot_lease_seconds) * 1000
    try:
        blocked_scope = await get_redis().eval(
            _ACQUIRE_SCRIPT,
            len(keys),
            *keys,
            now_ms,
            expires_ms,
            token,
            *limits,
        )
        if int(blocked_scope) != 0:
            return None
        return CapacityLease(token, keys, "redis")
    except Exception:
        logger.warning("Redis capacity coordination unavailable; using local fallback", exc_info=True)
        if not _acquire_local(keys, limits, token):
            return None
        return CapacityLease(token, keys, "local")


async def renew_chat_slot(lease: CapacityLease) -> bool:
    if lease.backend == "redis":
        expires_ms = int(time.time() * 1000) + max(1, settings.chat_slot_lease_seconds) * 1000
        try:
            renewed = await get_redis().eval(
                _RENEW_SCRIPT,
                len(lease.keys),
                *lease.keys,
                lease.token,
                expires_ms,
                max(2, settings.chat_slot_lease_seconds * 2) * 1000,
            )
            return bool(renewed)
        except Exception:
            return False
    with _local_guard:
        if any(lease.token not in _local_slots.get(key, {}) for key in lease.keys):
            return False
        deadline = time.monotonic() + max(1, settings.chat_slot_lease_seconds)
        for key in lease.keys:
            _local_slots[key][lease.token] = deadline
    return True


async def release_chat_slot(lease: CapacityLease) -> None:
    if lease.backend == "redis":
        try:
            await get_redis().eval(
                _RELEASE_SCRIPT,
                len(lease.keys),
                *lease.keys,
                lease.token,
            )
            return
        except Exception:
            logger.warning("Failed to release Redis capacity lease; TTL will recover it", exc_info=True)
            return
    with _local_guard:
        for key in lease.keys:
            slots = _local_slots.get(key)
            if slots is not None:
                slots.pop(lease.token, None)
                if not slots:
                    _local_slots.pop(key, None)


async def keep_chat_slot_alive(lease: CapacityLease) -> None:
    interval = max(1, min(30, settings.chat_slot_lease_seconds // 3))
    while True:
        await asyncio.sleep(interval)
        if not await renew_chat_slot(lease):
            return


def reset_local_slots_for_tests() -> None:
    with _local_guard:
        _local_slots.clear()
