"""数据库引擎与会话管理。

API 侧使用异步引擎（psycopg async），Celery worker 侧使用同步引擎。
两者共用同一套 ORM 模型。
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings


class Base(DeclarativeBase):
    pass


# ---- 异步引擎（FastAPI 使用）----
# Integration tests use several independent ``asyncio.run()`` event loops.  A
# pooled async connection is tied to the loop that created it, so disable the
# pool only for that explicit CI mode to prevent reusing a closed-loop socket.
_async_engine_options = {"pool_pre_ping": True}
if os.getenv("RUN_INTEGRATION") == "1":
    _async_engine_options["poolclass"] = NullPool
else:
    _async_engine_options.update(pool_size=10, max_overflow=20)

async_engine = create_async_engine(settings.database_url, **_async_engine_options)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    async with AsyncSessionLocal() as session:
        yield session


# ---- 同步引擎（Celery worker 使用）----
_sync_url = settings.database_url  # psycopg3 驱动同时支持同步与异步
sync_engine = create_engine(_sync_url, pool_size=5, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)
