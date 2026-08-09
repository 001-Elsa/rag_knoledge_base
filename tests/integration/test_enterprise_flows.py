"""Integration tests requiring PostgreSQL/pgvector and Redis."""

import asyncio
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
import redis.asyncio as aioredis
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import AsyncSessionLocal, SyncSessionLocal
from app.main import app
from app.models import (
    Chunk,
    Conversation,
    DocStatus,
    Document,
    KnowledgeBase,
    Organization,
    OutboxEvent,
    OutboxStatus,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.security import hash_password
from app.services import semantic_cache
from app.services.permissions import get_kb_with_permission
from app.services.resource_cleanup import delete_knowledge_base
from app.services.retriever import retrieve
from app.tasks import ingest as ingest_tasks
from app.tasks import outbox as outbox_tasks

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if os.getenv("RUN_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_INTEGRATION=1 with PostgreSQL and Redis available",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


def test_00_application_starts_and_health_is_sanitized():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def _tenant(db, suffix: str):
    owner = User(
        username=f"integration_owner_{suffix}",
        password_hash=hash_password("test"),
    )
    other = User(
        username=f"integration_other_{suffix}",
        password_hash=hash_password("test"),
    )
    db.add_all([owner, other])
    await db.flush()
    organization = Organization(
        name=f"integration_org_{suffix}", created_by=owner.id
    )
    db.add(organization)
    await db.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"integration_workspace_{suffix}",
    )
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=owner.id,
            role=WorkspaceRole.owner,
        )
    )
    kb = KnowledgeBase(
        owner_id=owner.id,
        workspace_id=workspace.id,
        name=f"integration_kb_{suffix}",
    )
    db.add(kb)
    await db.flush()
    return owner, other, organization, workspace, kb


def test_dedup_rbac_and_object_cleanup(tmp_path, monkeypatch):
    async def scenario():
        suffix = uuid.uuid4().hex[:8]
        filepath = tmp_path / "policy.txt"
        filepath.write_text("seven day return policy", encoding="utf-8")
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        monkeypatch.setattr(semantic_cache, "get_redis", lambda: redis_client)
        try:
            async with AsyncSessionLocal() as db:
                owner, other, organization, workspace, kb = await _tenant(db, suffix)
                document = Document(
                    owner_id=owner.id,
                    kb_id=kb.id,
                    filename="policy.txt",
                    filepath=str(filepath),
                    object_key=str(filepath),
                    content_hash="a" * 64,
                    size_bytes=filepath.stat().st_size,
                )
                db.add(document)
                await db.commit()
                document_id = document.id
                kb_id = kb.id
                workspace_id = workspace.id
                owner_id = owner.id
                other_id = other.id
                organization_id = organization.id

                duplicate = Document(
                    owner_id=owner.id,
                    kb_id=kb_id,
                    filename="renamed.txt",
                    filepath=str(tmp_path / "duplicate.txt"),
                    object_key=str(tmp_path / "duplicate.txt"),
                    content_hash="a" * 64,
                )
                db.add(duplicate)
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()

                with pytest.raises(HTTPException):
                    await get_kb_with_permission(db, kb_id, other_id, "read")
                db.add(
                    WorkspaceMembership(
                        workspace_id=workspace_id,
                        user_id=other_id,
                        role=WorkspaceRole.viewer,
                    )
                )
                await db.commit()
                assert (
                    await get_kb_with_permission(db, kb_id, other_id, "read")
                ).id == kb_id
                with pytest.raises(HTTPException):
                    await get_kb_with_permission(db, kb_id, other_id, "write")

                cache_key = f"sc:{owner_id}:{kb_id}"
                await redis_client.lpush(cache_key, "{}")
                kb = await db.get(KnowledgeBase, kb_id)
                await delete_knowledge_base(db, kb, owner_id)
                assert await db.get(Document, document_id) is None
                assert await redis_client.exists(cache_key) == 0
                # Object removal is asynchronous via resource.delete outbox (item 13).
                from app.models import OutboxEvent
                from app.tasks import maintenance as maintenance_tasks

                event = (
                    await db.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.event_type == "resource.delete.requested",
                            OutboxEvent.aggregate_id == kb_id,
                        )
                    )
                ).scalar_one_or_none()
                if event is not None:
                    maintenance_tasks.delete_resources.run(payload=event.payload)
                assert not filepath.exists()

                await db.delete(await db.get(Organization, organization_id))
                await db.delete(await db.get(User, other_id))
                await db.delete(await db.get(User, owner_id))
                await db.commit()
        finally:
            filepath.unlink(missing_ok=True)
            await redis_client.aclose()

    asyncio.run(scenario())


def test_outbox_dispatch_is_at_least_once_and_idempotent(tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "outbox.txt"
    filepath.write_text("outbox", encoding="utf-8")
    event_id = None
    organization_id = None
    owner_ids = []
    called = []

    async def prepare():
        nonlocal event_id, organization_id
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            owner_ids.extend([owner.id, other.id])
            organization_id = organization.id
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="b" * 64,
            )
            db.add(document)
            await db.flush()
            event = OutboxEvent(
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document.id,
                payload={},
                dedup_key=f"integration:{document.id}",
            )
            db.add(event)
            await db.commit()
            event_id = event.id
            return document.id

    document_id = asyncio.run(prepare())
    monkeypatch.setattr(
        outbox_tasks.ingest_document,
        "apply_async",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("broker unavailable")),
    )
    first = outbox_tasks.dispatch_outbox_batch.run()
    assert first["failed"] == 1
    with SyncSessionLocal() as db:
        event = db.get(OutboxEvent, event_id)
        assert event.status == OutboxStatus.pending
        assert event.retry_count == 1
        assert db.get(Document, document_id).status == DocStatus.uploaded
        event.next_retry_at = datetime.now(timezone.utc)  # noqa: UP017
        db.commit()

    monkeypatch.setattr(
        outbox_tasks.ingest_document,
        "apply_async",
        lambda **kwargs: called.append(kwargs),
    )
    second = outbox_tasks.dispatch_outbox_batch.run()
    duplicate_scan = outbox_tasks.dispatch_outbox_batch.run()
    assert second["sent"] == 1
    assert duplicate_scan["sent"] == 0
    assert len(called) == 1

    with SyncSessionLocal() as db:
        assert db.get(OutboxEvent, event_id).status == OutboxStatus.sent
        assert db.get(Document, document_id).status == DocStatus.queued
        organization = db.get(Organization, organization_id)
        db.delete(organization)
        for owner_id in owner_ids:
            db.delete(db.get(User, owner_id))
        db.commit()


def test_ingestion_version_switch_and_duplicate_delivery(tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "versioned.txt"
    filepath.write_text("versioned index", encoding="utf-8")

    async def prepare():
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="c" * 64,
                status=DocStatus.queued,
                stage=DocStatus.queued.value,
            )
            db.add(document)
            await db.commit()
            return document.id, organization.id, [owner.id, other.id]

    document_id, organization_id, owner_ids = asyncio.run(prepare())
    monkeypatch.setattr(
        ingest_tasks,
        "parse_file",
        lambda _: [("The return period is seven days.", 1)],
    )
    monkeypatch.setattr(
        ingest_tasks,
        "embed_documents",
        lambda texts: [[0.01] * settings.embedding_dim for _ in texts],
    )
    first = ingest_tasks.ingest_document.run(document_id)
    assert first["ok"] is True

    with SyncSessionLocal() as db:
        document = db.get(Document, document_id)
        assert document.status == DocStatus.ready
        assert document.active_index_version == 1
        assert document.chunk_count == 1
        document.status = DocStatus.queued
        document.stage = DocStatus.queued.value
        document.target_index_version = 2
        db.commit()

    second = ingest_tasks.ingest_document.run(document_id)
    duplicate = ingest_tasks.ingest_document.run(document_id)
    assert second["index_version"] == 2
    assert duplicate == {"ok": False, "reason": "status:ready"}

    with SyncSessionLocal() as db:
        document = db.get(Document, document_id)
        versions = list(
            db.execute(
                select(Chunk.index_version)
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.index_version)
            ).scalars()
        )
        assert document.active_index_version == 2
        assert versions == [1, 2]
        assert (
            db.execute(
                select(func.count()).select_from(Chunk).where(
                    Chunk.document_id == document_id,
                    Chunk.index_version == document.active_index_version,
                )
            ).scalar_one()
            == 1
        )
        db.delete(db.get(Organization, organization_id))
        for owner_id in owner_ids:
            db.delete(db.get(User, owner_id))
        db.commit()


def test_postgresql_rls_blocks_cross_tenant_rows():
    rls_url = os.getenv("RLS_DATABASE_URL")
    if not rls_url:
        pytest.skip("RLS_DATABASE_URL for a non-owner role is not configured")
    suffix = uuid.uuid4().hex[:8]

    async def prepare():
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            conversation = Conversation(
                owner_id=owner.id,
                kb_id=kb.id,
                title="RLS fixture",
            )
            db.add(conversation)
            await db.commit()
            return owner.id, other.id, organization.id, kb.id, conversation.id

    owner_id, other_id, organization_id, kb_id, conversation_id = asyncio.run(
        prepare()
    )
    rls_engine = create_engine(rls_url)
    try:
        with rls_engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": owner_id},
            )
            visible = connection.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :kb_id"),
                {"kb_id": kb_id},
            ).scalar_one_or_none()
            assert visible == kb_id
            assert (
                connection.execute(
                    text("SELECT id FROM conversations WHERE id = :id"),
                    {"id": conversation_id},
                ).scalar_one_or_none()
                == conversation_id
            )
        with rls_engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": other_id},
            )
            visible = connection.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :kb_id"),
                {"kb_id": kb_id},
            ).scalar_one_or_none()
            assert visible is None
            assert (
                connection.execute(
                    text("SELECT id FROM conversations WHERE id = :id"),
                    {"id": conversation_id},
                ).scalar_one_or_none()
                is None
            )

        def visible_for(user_id: str) -> tuple[str | None, str | None]:
            with rls_engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.user_id', :user_id, true)"),
                    {"user_id": user_id},
                )
                return (
                    connection.execute(
                        text("SELECT id FROM knowledge_bases WHERE id = :id"),
                        {"id": kb_id},
                    ).scalar_one_or_none(),
                    connection.execute(
                        text("SELECT id FROM conversations WHERE id = :id"),
                        {"id": conversation_id},
                    ).scalar_one_or_none(),
                )

        # Reuse the same connection pool concurrently. A tenant identity leaking
        # across pooled connections would make at least one of these assertions fail.
        identities = [owner_id, other_id] * 10
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(visible_for, identities))
        for identity, result in zip(identities, results):
            assert result == ((kb_id, conversation_id) if identity == owner_id else (None, None))
    finally:
        rls_engine.dispose()
        with SyncSessionLocal() as db:
            db.delete(db.get(Organization, organization_id))
            db.delete(db.get(User, other_id))
            db.delete(db.get(User, owner_id))
            db.commit()


def test_fixed_hybrid_retrieval_regression_fixture(tmp_path):
    suffix = uuid.uuid4().hex[:8]

    async def scenario():
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename="golden-policy.md",
                filepath=str(tmp_path / "golden-policy.md"),
                object_key=str(tmp_path / "golden-policy.md"),
                content_hash="d" * 64,
                status=DocStatus.ready,
                stage=DocStatus.ready.value,
                active_index_version=1,
                chunk_count=2,
            )
            db.add(document)
            await db.flush()
            refund_vector = [0.0] * settings.embedding_dim
            warranty_vector = [0.0] * settings.embedding_dim
            refund_vector[0] = 1.0
            warranty_vector[1] = 1.0
            db.add_all(
                [
                    Chunk(
                        document_id=document.id,
                        owner_id=owner.id,
                        kb_id=kb.id,
                        index_version=1,
                        seq=0,
                        parent_seq=0,
                        content="质量问题商品可以在签收后七日内退货。",
                        parent_content="质量问题商品可以在签收后七日内退货。",
                        content_tokens=func.to_tsvector("simple", "质量 问题 退款 期限 七日"),
                        embedding=refund_vector,
                    ),
                    Chunk(
                        document_id=document.id,
                        owner_id=owner.id,
                        kb_id=kb.id,
                        index_version=1,
                        seq=1,
                        parent_seq=1,
                        content="专业版产品提供二十四个月保修。",
                        parent_content="专业版产品提供二十四个月保修。",
                        content_tokens=func.to_tsvector("simple", "专业 产品 保修 二十四 月"),
                        embedding=warranty_vector,
                    ),
                ]
            )
            deleting_document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename="deleting-policy.md",
                filepath=str(tmp_path / "deleting-policy.md"),
                object_key=str(tmp_path / "deleting-policy.md"),
                content_hash="e" * 64,
                status=DocStatus.deleting,
                stage=DocStatus.deleting.value,
                active_index_version=1,
                chunk_count=1,
            )
            db.add(deleting_document)
            await db.flush()
            db.add(
                Chunk(
                    document_id=deleting_document.id,
                    owner_id=owner.id,
                    kb_id=kb.id,
                    index_version=1,
                    seq=0,
                    parent_seq=0,
                    content="这条删除中的退款规则不得再被检索。",
                    parent_content="这条删除中的退款规则不得再被检索。",
                    content_tokens=func.to_tsvector("simple", "退款 期限 删除"),
                    embedding=refund_vector,
                )
            )
            await db.commit()
            results = await retrieve(
                db,
                owner.id,
                "退款期限",
                kb_id=kb.id,
                query_vec=refund_vector,
                parent_child_enabled=False,
            )
            assert results
            assert results[0].seq == 0
            assert "七日内" in results[0].content
            assert all(result.document_id != deleting_document.id for result in results)
            await db.delete(await db.get(Organization, organization.id))
            await db.delete(await db.get(User, other.id))
            await db.delete(await db.get(User, owner.id))
            await db.commit()

    asyncio.run(scenario())
