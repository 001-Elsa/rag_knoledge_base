"""应用入口：FastAPI 实例、路由注册、中间件、指标、健康检查。"""
import asyncio
import contextvars
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

# Windows + psycopg 异步需要 SelectorEventLoop（默认 Proactor 不兼容）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.config import settings
from app.db import Base, async_engine, sync_engine
from app.limiter import limiter
from app.metrics import HTTP_DURATION, HTTP_REQUESTS, render_metrics
from app.observability import configure_observability
from app.routers import auth, chat, documents, kb, stats, tenancy, ws
from app.services.history import get_redis

# ---- 全链路请求 ID：contextvar 贯穿一次请求的所有日志，排查问题可按 ID 串起来 ----
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        try:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            record.trace_id = f"{context.trace_id:032x}" if context.is_valid else "-"
        except Exception:
            record.trace_id = "-"
        return True


logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s trace=%(trace_id)s] %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIdFilter())
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be configured with at least 32 characters")
    if settings.storage_backend.lower() in {"s3", "minio"} and (
        not settings.s3_access_key or not settings.s3_secret_key
    ):
        raise RuntimeError("S3_ACCESS_KEY and S3_SECRET_KEY are required")
    # 生产走 Alembic 迁移（alembic upgrade head）；本地开发可开 auto_create_tables 免迁移
    async with async_engine.begin() as conn:
        if settings.auto_create_tables:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        else:
            await conn.execute(text("SELECT 1"))
    logger.info("数据库初始化完成（auto_create_tables=%s）", settings.auto_create_tables)
    yield
    await async_engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
configure_observability(app, [async_engine.sync_engine, sync_engine])

# 限流
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS：仅当配置了白名单才启用（同源部署无需跨域，攻击面最小化）
if settings.cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# 请求 ID + 访问日志 + Prometheus 指标中间件
@app.middleware("http")
async def observability(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    request_id_var.set(rid)
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    path = request.scope.get("route").path if request.scope.get("route") else request.url.path
    HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    HTTP_DURATION.labels(request.method, path).observe(elapsed)
    logger.info("%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, elapsed * 1000)
    response.headers["X-Process-Time-Ms"] = f"{elapsed * 1000:.1f}"
    response.headers["X-Request-ID"] = rid
    # 基础安全响应头
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


app.include_router(auth.router)
app.include_router(kb.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(stats.router)
app.include_router(tenancy.router)
app.include_router(tenancy.organization_router)
app.include_router(ws.router)


@app.get("/api/health", tags=["运维"])
async def health():
    """健康检查：探测 PostgreSQL 与 Redis 连通性（供容器编排/负载均衡使用）。"""
    checks = {"database": "ok", "redis": "ok"}
    healthy = True
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "unavailable"
        healthy = False
    try:
        await get_redis().ping()
    except Exception:
        checks["redis"] = "unavailable"
        healthy = False
    status_code = 200 if healthy else 503
    return Response(
        content=json.dumps({"status": "ok" if healthy else "degraded", **checks}, ensure_ascii=False),
        media_type="application/json",
        status_code=status_code,
    )


@app.get("/metrics", tags=["运维"], include_in_schema=False)
async def metrics():
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)


# 前端
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse("app/static/index.html")
