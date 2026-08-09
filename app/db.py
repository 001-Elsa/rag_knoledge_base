"""数据库引擎与会话管理。

API 侧使用异步引擎（psycopg async），Celery worker 侧使用同步引擎。
两者共用同一套 ORM 模型。
"""
import os

from sqlalchemy import create_engine, event, text
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
    _async_engine_options.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
    )

async_engine = create_async_engine(settings.database_url, **_async_engine_options)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI 依赖：每个请求一个会话，租户上下文仅在事务内生效。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            session.info.pop("rls_user_id", None)
            try:
                await session.rollback()
            except Exception:
                # The connection may already be broken; closing the session will
                # invalidate it instead of returning tenant state to the pool.
                await session.invalidate()


# ---- 同步引擎（Celery worker 使用）----
_sync_url = settings.database_url  # psycopg3 驱动同时支持同步与异步
sync_engine = create_engine(
    _sync_url,
    pool_size=settings.worker_db_pool_size,
    pool_pre_ping=True,
    pool_timeout=settings.db_pool_timeout_seconds,
)
SyncSessionLocal = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _restore_transaction_tenant_context(session, _transaction, connection) -> None:
    """Reapply SET LOCAL after each commit without leaking identity through the pool."""
    user_id = session.info.get("rls_user_id")
    if user_id:
        connection.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": user_id},
        )


async def set_tenant_context(session: AsyncSession, user_id: str) -> None:
    """Bind a user to the current and all later transactions of one request."""
    session.info["rls_user_id"] = user_id
    await session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": user_id},
    )
