"""Authenticated, non-secret capability and technology-stack inventory."""

from fastapi import APIRouter, Depends

from app.config import settings
from app.deps import get_current_user
from app.models import User

router = APIRouter(prefix="/api/system", tags=["系统能力"])


@router.get("/capabilities")
async def capabilities(_user: User = Depends(get_current_user)):
    storage = "MinIO / S3" if settings.storage_backend.lower() in {"s3", "minio"} else "本地文件存储"
    return {
        "pipeline": [
            "数据接入",
            "解析与 OCR",
            "清洗切分",
            "Embedding",
            "混合检索",
            "Rerank",
            "上下文构建",
            "LLM 流式生成",
            "引用与拒答",
        ],
        "stack": [
            {"layer": "API", "technology": "FastAPI + Uvicorn", "purpose": "异步 API、JWT、SSE、WebSocket"},
            {"layer": "数据与向量", "technology": "PostgreSQL + pgvector", "purpose": "业务数据、RLS、HNSW 向量索引"},
            {"layer": "关键词检索", "technology": "PostgreSQL GIN + BM25", "purpose": "错误码、名称和专业术语精确召回"},
            {"layer": "缓存与队列", "technology": "Redis + Celery", "purpose": "异步入库、会话缓存、并发控制"},
            {"layer": "对象存储", "technology": storage, "purpose": "原始文件按 Workspace / 知识库隔离"},
            {"layer": "Embedding", "technology": settings.embedding_model, "purpose": f"多语言语义向量（{settings.embedding_dim} 维）"},
            {"layer": "生成模型", "technology": settings.llm_model, "purpose": "基于证据流式回答和追问建议"},
        ],
        "ingestion": ["PDF", "Word", "Excel", "网页", "Markdown", "数据库", "API", "图片与扫描件", "OCR", "表格解析"],
        "retrieval": ["向量检索", "BM25", "混合检索", "HNSW", "RRF", "规则重排", "父子文档", "Multi-Query", "HyDE", "Graph RAG"],
        "generation": ["多轮改写", "证据阈值", "拒答", "引用来源", "SSE 流式输出", "幻觉与 Prompt Injection 检查"],
        "security": ["JWT 双 Token", "RBAC", "Workspace RLS", "文档与 Chunk 权限", "手机号唯一约束", "审计哈希链", "敏感信息脱敏"],
    }
