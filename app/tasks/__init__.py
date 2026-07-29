"""Celery 应用实例。启动命令见 README（celery -A app.tasks worker）。"""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "rag_tasks",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.ingest", "app.tasks.outbox", "app.tasks.maintenance"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,           # 任务执行完才 ack，worker 崩溃时任务不丢
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # 向量化是重任务，避免一个 worker 囤积任务
    task_soft_time_limit=settings.ingestion_soft_time_limit_seconds,
    task_time_limit=settings.ingestion_hard_time_limit_seconds,
    beat_schedule={
        "dispatch-transactional-outbox": {
            "task": "dispatch_outbox_batch",
            "schedule": 2.0,
        },
        "reconcile-stale-ingestion": {
            "task": "reconcile_stale_ingestion",
            "schedule": 60.0,
        },
        "reconcile-orphan-objects": {
            "task": "reconcile_orphan_objects",
            "schedule": 86400.0,
            "args": [False],
        },
        "purge-expired-audit-logs": {
            "task": "purge_expired_audit_logs",
            "schedule": 86400.0,
        },
        "retry-pending-dead-letters": {
            "task": "retry_pending_dead_letters",
            "schedule": 1800.0,  # every 30 minutes
            "args": [1, 20],     # cooldown=1h, max 20 per run
        },
    },
)

from app.db import sync_engine  # noqa: E402
from app.observability import configure_worker_observability  # noqa: E402

configure_worker_observability(sync_engine)
