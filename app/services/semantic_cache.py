"""语义缓存：相似问题直接返回缓存答案，跳过检索与生成。

原理：
- 每个「用户 × 知识库范围」维护一个近期问答缓存（Redis List，LTRIM 限长 + TTL 过期）；
- 新问题先向量化，与缓存条目算余弦相似度，超过阈值（默认 0.95）视为同一问题直接命中；
- 命中收益：省一次检索 + 一次 LLM 生成，延迟从秒级降到几十毫秒，token 成本为零；
- 一致性：知识库内容变化（文档入库/删除）时整体失效该用户的缓存——宁可少命中，不可答旧数据。

为什么不用普通字符串缓存？"怎么退款" 和 "退款流程是什么" 字符串完全不同但语义相同，
只有向量相似度才能把它们命中到同一条缓存。
"""
import json
import logging
import math

from app.config import settings
from app.services.history import get_redis

logger = logging.getLogger(__name__)

_PREFIX = "sc:"


def _key(user_id: str, kb_id: str | None) -> str:
    return f"{_PREFIX}{user_id}:{kb_id or 'all'}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def lookup(user_id: str, kb_id: str | None, query_vec: list[float]) -> dict | None:
    """返回命中的缓存条目 {question, answer, sources, similarity}；未命中返回 None。"""
    if not settings.semantic_cache_enabled:
        return None
    try:
        raw_items = await get_redis().lrange(_key(user_id, kb_id), 0, -1)
    except Exception:
        return None  # Redis 故障降级为不走缓存
    best: dict | None = None
    best_sim = 0.0
    for raw in raw_items:
        try:
            item = json.loads(raw)
            sim = cosine_similarity(query_vec, item["vec"])
        except Exception:
            continue
        if sim > best_sim:
            best_sim, best = sim, item
    if best is not None and best_sim >= settings.semantic_cache_threshold:
        return {
            "question": best["question"],
            "answer": best["answer"],
            "sources": best["sources"],
            "similarity": round(best_sim, 4),
        }
    return None


async def store(
    user_id: str, kb_id: str | None, question: str, query_vec: list[float], answer: str, sources: list[dict]
) -> None:
    if not settings.semantic_cache_enabled or not answer:
        return
    try:
        key = _key(user_id, kb_id)
        entry = json.dumps(
            {"question": question, "vec": query_vec, "answer": answer, "sources": sources},
            ensure_ascii=False,
        )
        r = get_redis()
        pipe = r.pipeline()
        pipe.lpush(key, entry)
        pipe.ltrim(key, 0, settings.semantic_cache_size - 1)
        pipe.expire(key, settings.semantic_cache_ttl)
        await pipe.execute()
    except Exception:
        logger.warning("语义缓存写入失败", exc_info=True)


async def invalidate_user(user_id: str) -> None:
    """知识库内容变化时失效该用户全部语义缓存（异步版，API 进程用）。"""
    try:
        r = get_redis()
        async for key in r.scan_iter(match=f"{_PREFIX}{user_id}:*", count=100):
            await r.delete(key)
    except Exception:
        logger.warning("语义缓存失效失败", exc_info=True)


def invalidate_user_sync(user_id: str) -> None:
    """同步版（Celery worker 用）。"""
    try:
        from app.services.notify import _get_sync_client

        client = _get_sync_client()
        for key in client.scan_iter(match=f"{_PREFIX}{user_id}:*", count=100):
            client.delete(key)
    except Exception:
        logger.warning("语义缓存失效失败（sync）", exc_info=True)
