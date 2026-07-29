"""对话历史：PostgreSQL 持久化 + Redis 缓存加速。

设计：消息永远先落库（不丢数据），Redis 只作为热数据缓存（拼 Prompt 时免查库）。
缓存策略：写入时同步追加，读取 miss 时从库里回填（Cache-Aside）。
"""
import json

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Message

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the loop-bound client and let the next lifecycle create a fresh one."""
    global _redis
    client, _redis = _redis, None
    if client is not None:
        try:
            await client.aclose()
        except RuntimeError as exc:
            # A test client may already have closed the loop that owns the
            # connection.  The client has been detached above, so a later
            # lifecycle will create one bound to its own loop.
            if "Event loop is closed" not in str(exc):
                raise


def _key(conversation_id: str) -> str:
    return f"chat:history:{conversation_id}"


async def load_history(db: AsyncSession, conversation_id: str) -> list[dict]:
    """读取最近 N 轮历史（先 Redis，miss 则回源数据库并回填缓存）。"""
    max_messages = settings.history_max_turns * 2
    r = get_redis()
    try:
        cached = await r.lrange(_key(conversation_id), -max_messages, -1)
        if cached:
            return [json.loads(item) for item in cached]
    except Exception:
        pass  # Redis 故障时降级为直查数据库，不影响主流程

    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(max_messages)
    )
    rows = list((await db.execute(stmt)).scalars())[::-1]
    history = [{"role": m.role, "content": m.content} for m in rows]

    if history:
        try:
            pipe = r.pipeline()
            pipe.delete(_key(conversation_id))
            pipe.rpush(_key(conversation_id), *[json.dumps(h, ensure_ascii=False) for h in history])
            pipe.expire(_key(conversation_id), settings.history_cache_ttl)
            await pipe.execute()
        except Exception:
            pass
    return history


async def append_history(conversation_id: str, role: str, content: str) -> None:
    """向缓存追加一条消息（数据库写入由调用方负责）。"""
    try:
        r = get_redis()
        pipe = r.pipeline()
        pipe.rpush(_key(conversation_id), json.dumps({"role": role, "content": content}, ensure_ascii=False))
        pipe.ltrim(_key(conversation_id), -settings.history_max_turns * 2, -1)
        pipe.expire(_key(conversation_id), settings.history_cache_ttl)
        await pipe.execute()
    except Exception:
        pass
