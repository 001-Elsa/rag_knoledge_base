"""数据库引擎与会话管理。

API 侧使用异步引擎（psycopg async），Celery worker 侧使用同步引擎。
两者共用同一套 ORM 模型。
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


# ---- 异步引擎（FastAPI 使用）----
async_engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    async with AsyncSessionLocal() as session:
        yield session


# ---- 同步引擎（Celery worker 使用）----
_sync_url = settings.database_url  # psycopg3 驱动同时支持同步与异步
sync_engine = create_engine(_sync_url, pool_size=5, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)
