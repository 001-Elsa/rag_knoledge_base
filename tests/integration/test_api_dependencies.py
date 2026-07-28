"""Integration tests that require PostgreSQL/pgvector and Redis."""
import asyncio
import os
import sys
import uuid

import pytest
import redis.asyncio as aioredis
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import Document, KnowledgeBase, User
from app.routers.kb import get_owned_kb
from app.services import semantic_cache
from app.services import tokens as tokens_svc
from app.services.resource_cleanup import delete_knowledge_base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if os.getenv("RUN_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_INTEGRATION=1 with PostgreSQL and Redis available",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


def test_document_dedup_tenant_isolation_and_kb_cleanup(tmp_path, monkeypatch):
    async def scenario():
        suffix = uuid.uuid4().hex[:8]
        filepath = tmp_path / "policy.txt"
        filepath.write_text("seven day return policy", encoding="utf-8")
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        monkeypatch.setattr(semantic_cache, "get_redis", lambda: redis_client)
        owner_id = None
        other_id = None

        try:
            async with AsyncSessionLocal() as db:
                owner = User(username=f"integration_owner_{suffix}", password_hash="test")
                other = User(username=f"integration_other_{suffix}", password_hash="test")
                db.add_all([owner, other])
                await db.flush()
                owner_id = owner.id
                other_id = other.id
                kb = KnowledgeBase(owner_id=owner_id, name=f"integration_kb_{suffix}")
                db.add(kb)
                await db.flush()
                document = Document(
                    owner_id=owner_id,
                    kb_id=kb.id,
                    filename="policy.txt",
                    filepath=str(filepath),
                    content_hash="a" * 64,
                )
                db.add(document)
                await db.commit()
                kb_id = kb.id
                document_id = document.id

                duplicate = Document(
                    owner_id=owner_id,
                    kb_id=kb_id,
                    filename="renamed.txt",
                    filepath=str(tmp_path / "duplicate.txt"),
                    content_hash="a" * 64,
                )
                db.add(duplicate)
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()

                with pytest.raises(HTTPException) as exc_info:
                    await get_owned_kb(db, kb_id, other_id)
                assert exc_info.value.status_code == 404

                cache_key = f"sc:{owner_id}:all"
                await redis_client.lpush(cache_key, "{}")
                kb = await db.get(KnowledgeBase, kb_id)
                await delete_knowledge_base(db, kb, owner_id)

                assert not filepath.exists()
                assert await db.get(Document, document_id) is None
                assert await redis_client.exists(cache_key) == 0
        finally:
            async with AsyncSessionLocal() as cleanup_db:
                for user_id in (owner_id, other_id):
                    if user_id and (user := await cleanup_db.get(User, user_id)):
                        await cleanup_db.delete(user)
                await cleanup_db.commit()
            filepath.unlink(missing_ok=True)
            await redis_client.aclose()

    asyncio.run(scenario())


def test_refresh_token_rotation_is_atomic_with_real_redis(monkeypatch):
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    monkeypatch.setattr(tokens_svc, "get_redis", lambda: redis_client)

    async def scenario():
        _, refresh = await tokens_svc.issue_token_pair("integration-user")
        results = await asyncio.gather(
            tokens_svc.rotate_refresh_token(refresh),
            tokens_svc.rotate_refresh_token(refresh),
        )
        assert sum(result is not None for result in results) == 1
        for result in results:
            if result is not None:
                await tokens_svc.revoke_refresh_token(result[1])
        await redis_client.aclose()

    asyncio.run(scenario())
