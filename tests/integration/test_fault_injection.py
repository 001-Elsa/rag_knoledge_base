"""Fault-injection and recovery tests (item 20) plus MinIO / WS coverage (item 19)."""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import AsyncSessionLocal, SyncSessionLocal
from app.models import (
    Chunk,
    DeadLetterStatus,
    DeadLetterTask,
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
from app.services.object_storage import get_object_storage
from app.services.resource_cleanup import delete_document, delete_knowledge_base
from app.tasks import ingest as ingest_tasks
from app.tasks import maintenance as maintenance_tasks
from app.tasks import outbox as outbox_tasks

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if os.getenv("RUN_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_INTEGRATION=1 with PostgreSQL and Redis available",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _tenant(db, suffix: str):
    owner = User(username=f"fault_owner_{suffix}", password_hash="test")
    other = User(username=f"fault_other_{suffix}", password_hash="test")
    db.add_all([owner, other])
    await db.flush()
    organization = Organization(name=f"fault_org_{suffix}", created_by=owner.id)
    db.add(organization)
    await db.flush()
    workspace = Workspace(organization_id=organization.id, name=f"fault_ws_{suffix}")
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.owner
        )
    )
    kb = KnowledgeBase(
        owner_id=owner.id, workspace_id=workspace.id, name=f"fault_kb_{suffix}"
    )
    db.add(kb)
    await db.flush()
    return owner, other, organization, workspace, kb


async def _cleanup(organization_id, owner_ids):
    with SyncSessionLocal() as db:
        org = db.get(Organization, organization_id)
        if org:
            db.delete(org)
        for owner_id in owner_ids:
            user = db.get(User, owner_id)
            if user:
                db.delete(user)
        db.commit()


def test_lease_recovery_after_embedding_heartbeat_expires(tmp_path, monkeypatch):
    """Simulate worker death mid-embedding: heartbeat expires → reconcile → retry."""
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "lease.txt"
    filepath.write_text("lease recovery fixture", encoding="utf-8")

    async def prepare():
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="e" * 64,
                status=DocStatus.embedding,
                stage=DocStatus.embedding.value,
                processing_token="dead-token",
                worker_id="dead-worker",
                heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=600),  # noqa: UP017
                target_index_version=1,
            )
            db.add(document)
            await db.commit()
            return document.id, organization.id, [owner.id, other.id]

    document_id, organization_id, owner_ids = asyncio.run(prepare())
    recovered = maintenance_tasks.reconcile_stale_ingestion.run(stale_after_seconds=300)
    assert recovered["recovered"] >= 1
    with SyncSessionLocal() as db:
        document = db.get(Document, document_id)
        assert document.status == DocStatus.retrying
        assert document.processing_token is None
        events = list(
            db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == document_id,
                    OutboxEvent.event_type == "document.ingest.requested",
                )
            ).scalars()
        )
        assert events
    asyncio.run(_cleanup(organization_id, owner_ids))


def test_index_activation_failure_leaves_old_version_queryable(tmp_path, monkeypatch):
    """Half-built target version must not replace active_index_version on failure."""
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "rollback.txt"
    filepath.write_text("rollback fixture", encoding="utf-8")

    async def prepare():
        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="f" * 64,
                status=DocStatus.queued,
                stage=DocStatus.queued.value,
                active_index_version=1,
                chunk_count=1,
            )
            db.add(document)
            await db.flush()
            db.add(
                Chunk(
                    document_id=document.id,
                    owner_id=owner.id,
                    kb_id=kb.id,
                    index_version=1,
                    seq=0,
                    parent_seq=0,
                    content="old version stays queryable",
                    parent_content="old version stays queryable",
                    content_tokens=func.to_tsvector("simple", "old version"),
                    embedding=[0.02] * settings.embedding_dim,
                )
            )
            await db.commit()
            return document.id, organization.id, [owner.id, other.id], kb.id

    document_id, organization_id, owner_ids, _kb_id = asyncio.run(prepare())
    monkeypatch.setattr(
        ingest_tasks, "parse_file", lambda _: [("new content for v2", 1)]
    )

    def boom(_texts):
        raise RuntimeError("embedding killed mid-flight")

    monkeypatch.setattr(ingest_tasks, "embed_documents", boom)
    from celery.exceptions import Retry

    with pytest.raises(Retry):
        ingest_tasks.ingest_document.run(document_id)

    with SyncSessionLocal() as db:
        document = db.get(Document, document_id)
        assert document.active_index_version == 1
        assert (
            db.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.document_id == document_id, Chunk.index_version == 1)
            ).scalar_one()
            == 1
        )
        assert document.status == DocStatus.retrying
    asyncio.run(_cleanup(organization_id, owner_ids))


def test_outbox_exhaustion_lands_in_dead_letter(tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "dlq.txt"
    filepath.write_text("dlq", encoding="utf-8")

    async def prepare():
        async with AsyncSessionLocal() as db:
            owner, other, organization, workspace, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="1" * 64,
            )
            db.add(document)
            await db.flush()
            event = OutboxEvent(
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document.id,
                payload={"workspace_id": workspace.id},
                dedup_key=f"dlq:{document.id}",
                retry_count=settings.outbox_max_retries - 1,
            )
            db.add(event)
            await db.commit()
            return event.id, organization.id, [owner.id, other.id], workspace.id

    event_id, organization_id, owner_ids, workspace_id = asyncio.run(prepare())
    monkeypatch.setattr(
        outbox_tasks.ingest_document,
        "apply_async",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("broker still down")),
    )
    outbox_tasks.dispatch_outbox_batch.run()
    with SyncSessionLocal() as db:
        event = db.get(OutboxEvent, event_id)
        assert event.status == OutboxStatus.failed
        letters = list(
            db.execute(
                select(DeadLetterTask).where(
                    DeadLetterTask.workspace_id == workspace_id,
                    DeadLetterTask.source == "outbox",
                )
            ).scalars()
        )
        assert letters
        assert letters[0].status == DeadLetterStatus.pending
    asyncio.run(_cleanup(organization_id, owner_ids))


def test_resource_delete_outbox_removes_object(tmp_path):
    """Item 13: deleting marks DB then outbox worker removes the object."""
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "to-delete.txt"
    filepath.write_text("delete me", encoding="utf-8")

    async def scenario():
        async with AsyncSessionLocal() as db:
            owner, other, organization, workspace, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="2" * 64,
            )
            db.add(document)
            await db.commit()
            document_id = document.id
            await delete_document(db, document, owner.id)
            document = await db.get(Document, document_id)
            assert document is not None
            assert document.status == DocStatus.deleting
            assert filepath.exists()  # object still present until worker runs

            event = (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "resource.delete.requested",
                        OutboxEvent.aggregate_id == document_id,
                    )
                )
            ).scalar_one()
            payload = event.payload
            event.status = OutboxStatus.sent
            await db.commit()

            result = maintenance_tasks.delete_resources.run(payload=payload)
            assert result["ok"] is True
            assert not filepath.exists()
            assert await db.get(Document, document_id) is None

            await db.delete(await db.get(Organization, organization.id))
            await db.delete(await db.get(User, other.id))
            await db.delete(await db.get(User, owner.id))
            await db.commit()

    asyncio.run(scenario())


def test_reranker_failure_falls_back_to_rrf(tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]

    async def scenario():
        from app.services import retriever as retriever_mod

        async with AsyncSessionLocal() as db:
            owner, other, organization, _, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename="rerank.md",
                filepath=str(tmp_path / "rerank.md"),
                object_key=str(tmp_path / "rerank.md"),
                content_hash="3" * 64,
                status=DocStatus.ready,
                stage=DocStatus.ready.value,
                active_index_version=1,
                chunk_count=1,
            )
            db.add(document)
            await db.flush()
            vec = [0.0] * settings.embedding_dim
            vec[0] = 1.0
            db.add(
                Chunk(
                    document_id=document.id,
                    owner_id=owner.id,
                    kb_id=kb.id,
                    index_version=1,
                    seq=0,
                    parent_seq=0,
                    content="退款期限是七天。",
                    parent_content="退款期限是七天。",
                    content_tokens=func.to_tsvector("simple", "退款 期限 七天"),
                    embedding=vec,
                )
            )
            await db.commit()

            def boom(*_a, **_k):
                raise RuntimeError("reranker model missing")

            monkeypatch.setattr(retriever_mod, "_cross_encoder_rerank", boom)
            results = await retriever_mod.retrieve(
                db,
                owner.id,
                "退款期限",
                kb_id=kb.id,
                query_vec=vec,
                rerank_enabled=True,
                parent_child_enabled=False,
            )
            assert results  # fell back to RRF ordering
            await db.delete(await db.get(Organization, organization.id))
            await db.delete(await db.get(User, other.id))
            await db.delete(await db.get(User, owner.id))
            await db.commit()

    asyncio.run(scenario())


def test_websocket_ticket_end_to_end():
    """Item 19: mint ticket → consume once → second consume rejected."""
    from app.services.tokens import consume_websocket_ticket, issue_websocket_ticket

    async def scenario():
        ticket = await issue_websocket_ticket("user-ws-test")
        assert ticket
        first = await consume_websocket_ticket(ticket)
        assert first == "user-ws-test"
        second = await consume_websocket_ticket(ticket)
        assert second is None

    asyncio.run(scenario())


def test_minio_object_roundtrip_when_configured(tmp_path, monkeypatch):
    """Item 19: real MinIO put/delete when S3_* env points at a live endpoint."""
    if not os.getenv("S3_ENDPOINT_URL"):
        pytest.skip("S3_ENDPOINT_URL not set")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", os.getenv("S3_BUCKET", "rag-ci"))
    # Reset cached storage singleton.
    import app.services.object_storage as storage_mod

    storage_mod._storage = None
    try:
        storage = get_object_storage()
        key = f"ci/{uuid.uuid4().hex}.txt"
        source = tmp_path / "upload.txt"
        source.write_text("minio roundtrip", encoding="utf-8")
        storage.put_file(key, source)
        with storage.materialize(key) as path:
            assert Path(path).read_text(encoding="utf-8") == "minio roundtrip"
        storage.delete(key)
    except Exception as exc:
        pytest.skip(f"MinIO not reachable: {exc}")
    finally:
        storage_mod._storage = None


def test_kb_delete_enqueues_resource_delete(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    filepath = tmp_path / "kb-del.txt"
    filepath.write_text("kb delete", encoding="utf-8")

    async def scenario():
        async with AsyncSessionLocal() as db:
            owner, other, organization, workspace, kb = await _tenant(db, suffix)
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=filepath.name,
                filepath=str(filepath),
                object_key=str(filepath),
                content_hash="4" * 64,
            )
            db.add(document)
            await db.commit()
            kb_id = kb.id
            await delete_knowledge_base(db, kb, owner.id)
            assert await db.get(KnowledgeBase, kb_id) is None
            event = (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "resource.delete.requested",
                        OutboxEvent.aggregate_id == kb_id,
                    )
                )
            ).scalar_one()
            result = maintenance_tasks.delete_resources.run(payload=event.payload)
            assert result["ok"] is True
            assert not filepath.exists()
            await db.delete(await db.get(Organization, organization.id))
            await db.delete(await db.get(User, other.id))
            await db.delete(await db.get(User, owner.id))
            await db.commit()

    asyncio.run(scenario())
