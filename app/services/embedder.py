"""本地 Embedding 服务：bge-small-zh-v1.5（sentence-transformers）。

- 模型进程内单例、懒加载：首次调用才加载，避免 API 启动被拖慢；
- 国内下载 HuggingFace 模型慢/失败时，设置环境变量 HF_ENDPOINT=https://hf-mirror.com；
- bge 检索场景约定：文档向量直接编码，查询向量需加指令前缀。
"""
import threading
from collections import OrderedDict

from app.config import settings
from app.metrics import EMBEDDING_CACHE_HITS

_model = None
_lock = threading.Lock()
_cache: OrderedDict[str, list[float]] = OrderedDict()


def _cache_get(key: str, kind: str) -> list[float] | None:
    with _lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
            EMBEDDING_CACHE_HITS.labels(kind).inc()
            return list(value)
    return None


def _cache_put(key: str, value: list[float]) -> None:
    if settings.embedding_cache_size <= 0:
        return
    with _lock:
        _cache[key] = list(value)
        _cache.move_to_end(key)
        while len(_cache) > settings.embedding_cache_size:
            _cache.popitem(last=False)


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:  # 双重检查，避免并发重复加载
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(settings.embedding_model, device="cpu")
    return _model


def embed_documents(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """文档入库向量化（批量）。"""
    if not texts:
        return []
    results: list[list[float] | None] = [None] * len(texts)
    missing_indexes = []
    missing_texts = []
    for index, text in enumerate(texts):
        key = f"document:{settings.embedding_model}:{text}"
        cached = _cache_get(key, "document")
        if cached is None:
            missing_indexes.append(index)
            missing_texts.append(text)
        else:
            results[index] = cached
    if missing_texts:
        model = _get_model()
        vectors = model.encode(
            missing_texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        for index, text, vector in zip(missing_indexes, missing_texts, vectors):
            value = vector.tolist()
            results[index] = value
            _cache_put(f"document:{settings.embedding_model}:{text}", value)
    return [value for value in results if value is not None]


def embed_query(query: str) -> list[float]:
    """查询向量化：bge 系列加检索指令前缀效果更好。"""
    value = settings.embedding_query_instruction + query
    key = f"query:{settings.embedding_model}:{value}"
    cached = _cache_get(key, "query")
    if cached is not None:
        return cached
    model = _get_model()
    vector = model.encode(value, normalize_embeddings=True).tolist()
    _cache_put(key, vector)
    return vector
