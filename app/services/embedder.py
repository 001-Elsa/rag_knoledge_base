"""本地 Embedding 服务：bge-small-zh-v1.5（sentence-transformers）。

- 模型进程内单例、懒加载：首次调用才加载，避免 API 启动被拖慢；
- 国内下载 HuggingFace 模型慢/失败时，设置环境变量 HF_ENDPOINT=https://hf-mirror.com；
- bge 检索场景约定：文档向量直接编码，查询向量需加指令前缀。
"""
import threading

from app.config import settings

_model = None
_lock = threading.Lock()


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
    model = _get_model()
    vectors = model.encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def embed_query(query: str) -> list[float]:
    """查询向量化：bge 系列加检索指令前缀效果更好。"""
    model = _get_model()
    vector = model.encode(settings.embedding_query_instruction + query, normalize_embeddings=True)
    return vector.tolist()
