"""全局配置：所有配置项都可通过环境变量或 .env 文件覆盖。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- 应用 ----
    app_name: str = "RAG 知识库问答系统"
    debug: bool = False
    upload_dir: str = "uploads"
    max_upload_mb: int = 20
    max_document_pages: int = 500
    max_uncompressed_mb: int = 100
    workspace_storage_quota_mb: int = 10240
    allowed_mime_types: str = (
        "application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "text/plain,text/markdown,text/x-markdown,application/octet-stream"
    )

    # ---- 对象存储（local / s3；MinIO 使用 S3 兼容协议）----
    storage_backend: str = "local"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "rag-documents"
    s3_region: str = "us-east-1"
    s3_secure: bool = False

    # ---- 数据库 ----
    database_url: str = "postgresql+psycopg://rag:ragpass@localhost:5432/ragdb"
    # Celery worker 使用同步驱动，与 API 共用同一个 URL

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- JWT（双 Token：短效 Access + 长效 Refresh，Refresh 存 Redis 支持吊销/轮换）----
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # ---- DeepSeek（OpenAI 兼容协议）----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048
    # LLM 容错：超时 / 重试 / 熔断
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 1              # 非流式调用失败重试次数
    breaker_fail_threshold: int = 5       # 连续失败 N 次后熔断
    breaker_cooldown_seconds: int = 30    # 熔断冷却时长

    # ---- Agent 模式 ----
    agent_max_steps: int = 4              # 工具调用循环上限，防止死循环烧钱

    # ---- 备用模型（多模型容灾）：主模型重试失败/熔断时自动切换，留空则不启用 ----
    llm_fallback_api_key: str = ""
    llm_fallback_base_url: str = ""       # 任意 OpenAI 兼容服务（如硅基流动/智谱）
    llm_fallback_model: str = ""

    # ---- 长对话滚动摘要 ----
    history_summary_enabled: bool = True
    summary_trigger_messages: int = 12    # 消息总数超过该值开始做摘要
    summary_keep_recent: int = 6          # 最近 N 条不进摘要（原文进 Prompt）

    # ---- Embedding（本地模型）----
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512  # bge-small-zh-v1.5 的输出维度
    # bge 系列检索查询需加指令前缀，效果更好
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："

    # ---- 切片 ----
    chunk_size: int = 512      # 每片最大字符数
    chunk_overlap: int = 64    # 相邻片重叠字符数

    # ---- 检索 ----
    retrieval_top_k: int = 5          # 最终送入 LLM 的片段数
    retrieval_candidates: int = 30    # 每路召回的候选数
    rrf_k: int = 60                   # RRF 融合常数
    rerank_enabled: bool = False      # 交叉编码器重排（需额外下载约 1GB 模型）
    rerank_model: str = "BAAI/bge-reranker-base"
    parent_child_enabled: bool = True

    # ---- 对话 ----
    history_max_turns: int = 6        # 携带进 Prompt 的历史轮数
    history_cache_ttl: int = 3600     # Redis 对话历史缓存秒数
    query_rewrite_enabled: bool = True   # 多轮对话中自动改写省略指代的问题
    suggestions_enabled: bool = True     # 回答后生成推荐追问
    multi_query_enabled: bool = False    # 多查询扩展召回（效果↑ 延迟↑，评估后按需开启）

    # ---- 语义缓存 ----
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95  # 余弦相似度阈值，高于此值视为同一问题
    semantic_cache_size: int = 50           # 每个 用户×知识库 保留的缓存条数
    semantic_cache_ttl: int = 86400         # 缓存有效期（秒）

    # ---- 证据控制 ----
    evidence_min_chunks: int = 1
    evidence_min_rrf_score: float = 0.012
    prompt_injection_detection_enabled: bool = True

    # ---- 限流 ----
    rate_limit_chat: str = "20/minute"
    rate_limit_upload: str = "10/minute"
    rate_limit_storage_url: str = ""  # 留空=进程内存；多实例部署配置为 Redis URL

    # ---- 运维 ----
    auto_create_tables: bool = True   # 本地开发免迁移直接建表；生产用 Alembic 时设为 false
    cors_origins: str = ""            # 跨域白名单，逗号分隔；留空=不启用 CORS（同源部署）
    refresh_cookie_name: str = "rag_refresh"
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: str = "lax"
    websocket_ticket_ttl_seconds: int = 60
    expose_refresh_token_in_body: bool = False

    # ---- 可靠任务与可观测性 ----
    outbox_batch_size: int = 50
    outbox_max_retries: int = 12
    outbox_base_retry_seconds: int = 2
    orphan_object_grace_seconds: int = 86400
    audit_retention_days: int = 365
    ingestion_soft_time_limit_seconds: int = 25 * 60
    ingestion_hard_time_limit_seconds: int = 30 * 60
    otel_service_name: str = "rag-api"
    otel_exporter_otlp_endpoint: str = ""
    worker_metrics_port: int = 9101


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
