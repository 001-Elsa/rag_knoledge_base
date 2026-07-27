"""Prometheus 指标：HTTP 请求 + RAG 关键阶段延迟。

暴露在 GET /metrics，可直接对接 Prometheus + Grafana。
"""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

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
LLM_FIRST_TOKEN = Histogram(
    "llm_first_token_seconds", "LLM 首 token 延迟",
    buckets=(0.2, 0.5, 1, 2, 3, 5, 10),
)
QA_TOTAL = Counter("rag_questions_total", "问答总次数")


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
