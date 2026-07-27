"""Celery 应用实例。启动命令见 README（celery -A app.tasks worker）。"""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "rag_tasks",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.ingest"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,           # 任务执行完才 ack，worker 崩溃时任务不丢
    worker_prefetch_multiplier=1,  # 向量化是重任务，避免一个 worker 囤积任务
    task_time_limit=60 * 30,       # 单文档处理硬超时 30 分钟
)
