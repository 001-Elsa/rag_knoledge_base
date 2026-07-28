"""实时通知：Redis 发布/订阅。

- Celery worker（同步进程）在文档状态变化时 publish；
- API 的 WebSocket 端点 subscribe 对应用户的频道，推给浏览器；
- 频道按用户隔离：notify:{user_id}。
"""
import json
import logging

import redis as sync_redis

from app.config import settings

logger = logging.getLogger(__name__)

_sync_client: sync_redis.Redis | None = None


def channel_for(user_id: str) -> str:
    return f"notify:{user_id}"


def _get_sync_client() -> sync_redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = sync_redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _sync_client


def publish_sync(user_id: str, payload: dict) -> None:
    """同步发布（Celery worker 用）。失败只记日志，不影响主流程。"""
    try:
        _get_sync_client().publish(channel_for(user_id), json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.warning("通知发布失败（Redis 不可用？）", exc_info=True)


def document_event(
    document_id: str,
    status: str,
    *,
    filename: str = "",
    chunk_count: int = 0,
    error: str | None = None,
    **details,
) -> dict:
    return {
        "type": "document",
        "document_id": document_id,
        "filename": filename,
        "status": status,
        "chunk_count": chunk_count,
        "error": error,
        **details,
    }
