"""Prometheus 指标：HTTP 请求 + RAG 关键阶段延迟。

暴露在 GET /metrics，可直接对接 Prometheus + Grafana。
"""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# HTTP 层
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP 请求数", ["method", "path", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP 请求耗时", ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.3, 0.5, 1, 3, 5, 10, 30),
)

# RAG 关键阶段
RETRIEVAL_DURATION = Histogram(
    "rag_retrieval_duration_seconds", "混合检索耗时（含查询向量化）",
    buckets=(0.05, 0.1, 0.3, 0.5, 1, 2, 5),
)
CONTEXT_BUILD_DURATION = Histogram(
    "rag_context_build_duration_seconds", "上下文去重、压缩和预算耗时",
    buckets=(0.001, 0.005, 0.01, 0.03, 0.1, 0.3, 1),
)
RERANK_DURATION = Histogram(
    "rag_rerank_duration_seconds", "重排耗时", ["kind"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.3, 1, 3, 10),
)
LLM_GENERATION_DURATION = Histogram(
    "llm_generation_duration_seconds", "LLM 完整生成耗时",
    buckets=(0.2, 0.5, 1, 2, 3, 5, 10, 30, 60),
)
RAG_END_TO_END_DURATION = Histogram(
    "rag_end_to_end_duration_seconds", "RAG 请求端到端耗时",
    buckets=(0.2, 0.5, 1, 2, 3, 5, 10, 30, 60),
)
LLM_FIRST_TOKEN = Histogram(
    "llm_first_token_seconds", "LLM 首 token 延迟",
    buckets=(0.2, 0.5, 1, 2, 3, 5, 10),
)
QA_TOTAL = Counter("rag_questions_total", "问答总次数")
NO_ANSWER_TOTAL = Counter("rag_no_answer_total", "证据不足而拒答的次数")
CITATION_VALIDATION_FAILURES = Counter(
    "rag_citation_validation_failures_total", "回答引用校验失败次数"
)
SEMANTIC_CACHE_HITS = Counter("semantic_cache_hits_total", "语义缓存命中次数")
SEMANTIC_CACHE_LOOKUPS = Counter("semantic_cache_lookups_total", "语义缓存查询次数")
RAG_QUERY_ROUTES = Counter("rag_query_routes_total", "查询路由次数", ["route", "reason"])
RAG_FEEDBACK = Counter("rag_answer_feedback_total", "用户答案反馈", ["rating", "reason"])
RAG_USER_INTERACTIONS = Counter(
    "rag_user_interactions_total", "用户与答案交互", ["action"]
)
CONTEXT_CHUNKS = Histogram(
    "rag_context_chunks", "最终上下文切片数量", buckets=(1, 2, 3, 5, 8, 13, 21)
)
LLM_TOKEN_USAGE = Counter(
    "llm_token_usage_total", "LLM token 用量", ["model", "kind"]
)
LLM_FALLBACK_TOTAL = Counter("llm_fallback_total", "备用模型切换次数", ["model"])
EMBEDDING_CACHE_HITS = Counter("embedding_cache_hits_total", "Embedding 进程缓存命中次数", ["kind"])
INGESTION_STAGE_DURATION = Histogram(
    "ingestion_stage_duration_seconds",
    "文档入库阶段耗时",
    ["stage"],
    buckets=(0.05, 0.1, 0.5, 1, 3, 10, 30, 60, 180, 600),
)
INGESTION_FAILURES = Counter(
    "ingestion_failure_total", "文档入库失败次数", ["retryable"]
)
INGESTION_RETRIES = Counter("celery_retry_total", "Celery 入库任务重试次数")
INGESTION_COMPLETED = Counter("ingestion_completed_total", "文档入库成功次数")
INGESTION_QUEUE_DELAY = Histogram(
    "ingestion_queue_delay_seconds",
    "文档从上传到 Worker 抢占的排队时长",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)
OUTBOX_PENDING = Gauge("outbox_pending_events", "等待或正在投递的 Outbox 事件数")
OUTBOX_DISPATCH_FAILURES = Counter(
    "outbox_dispatch_failures_total", "Outbox 投递失败次数"
)
DEAD_LETTER_TOTAL = Counter(
    "dead_letter_tasks_total", "进入死信队列的任务数", ["source"]
)
DEAD_LETTER_REPLAYED = Counter(
    "dead_letter_replayed_total", "管理员重放死信任务次数"
)
RESOURCE_DELETE_FAILURES = Counter(
    "resource_delete_failures_total", "对象存储删除任务失败次数"
)
CITATION_ENTAILMENT_FAILURES = Counter(
    "rag_citation_entailment_failures_total", "Claim-Evidence 语义校验失败次数"
)
CITATION_ENTAILMENT_UNAVAILABLE = Counter(
    "rag_citation_entailment_unavailable_total", "语义校验服务不可用（降级）次数"
)
PROMPT_INJECTION_SUSPECTED = Counter(
    "prompt_injection_suspected_total", "检索/入库中检出的可疑内容次数", ["level"]
)
PROMPT_INJECTION_CHUNKS_ACTED = Counter(
    "prompt_injection_chunks_acted_total", "对可疑切片执行处置的次数", ["action"]
)
PROMPT_INJECTION_QUARANTINED = Counter(
    "prompt_injection_quarantined_total", "入库时被隔离的文档数"
)
TENANT_STORAGE_USAGE = Gauge(
    "tenant_storage_usage_bytes", "租户对象存储使用量", ["workspace_id"]
)
CHAT_CONCURRENCY_ACTIVE = Gauge(
    "rag_chat_concurrency_active", "当前 API 实例正在处理的流式问答数"
)
CHAT_CONCURRENCY_REJECTED = Counter(
    "rag_chat_concurrency_rejected_total", "并发容量保护拒绝次数", ["scope"]
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
