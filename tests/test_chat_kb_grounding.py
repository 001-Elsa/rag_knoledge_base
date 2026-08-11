from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException, status

from app.routers import chat as chat_router
from app.schemas import ChatRequest


def test_chat_capacity_rejects_missing_knowledge_base_before_acquiring(monkeypatch):
    async def fail_acquire(*_args, **_kwargs):
        raise AssertionError("capacity must not be acquired without kb_id")

    monkeypatch.setattr(chat_router, "acquire_chat_slot", fail_acquire)

    with pytest.raises(HTTPException) as exc:
        anyio.run(
            chat_router._chat_capacity,
            ChatRequest(question="这个项目做什么"),
            SimpleNamespace(id="u1"),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert "知识库" in exc.value.detail


def test_scope_chunks_to_kb_drops_cross_kb_context():
    chunks = [
        SimpleNamespace(chunk_id="a", kb_id="kb-a", content="A"),
        SimpleNamespace(chunk_id="b", kb_id="kb-b", content="B"),
        SimpleNamespace(chunk_id="missing", content="legacy"),
    ]

    scoped = chat_router._scope_chunks_to_kb(chunks, "kb-a")

    assert [chunk.chunk_id for chunk in scoped] == ["a"]


def test_chunk_source_preserves_knowledge_base_id():
    source = chat_router._chunk_to_source(
        SimpleNamespace(
            kb_id="kb-a",
            document_id="doc-a",
            filename="README.md",
            seq=0,
            page=None,
            content="actual file content",
            score=0.9,
            vector_similarity=0.8,
            keyword_hit=True,
            section="Overview",
            content_type="text",
            source_url=None,
            chunk_id="chunk-a",
            created_at=None,
        )
    )

    assert source["kb_id"] == "kb-a"
    assert source["content"] == "actual file content"


def test_grounded_fallback_only_uses_retrieved_chunks_with_citations():
    chunks = [
        SimpleNamespace(
            document_name="README.md",
            section="技术栈",
            content="FastAPI PostgreSQL pgvector Redis Celery",
        ),
        SimpleNamespace(
            document_name="部署.md",
            section=None,
            content="Docker Compose 与 OpenTelemetry",
        ),
    ]

    answer = chat_router._build_grounded_fallback(chunks)

    assert "直接摘自当前知识库" in answer
    assert "README.md · 技术栈" in answer
    assert "FastAPI PostgreSQL pgvector Redis Celery [1]" in answer
    assert "Docker Compose 与 OpenTelemetry [2]" in answer
    assert chat_router._has_substantive_answer(answer)


def test_citation_only_output_is_not_a_substantive_answer():
    assert not chat_router._has_substantive_answer("[1][5]")
    assert not chat_router._has_substantive_answer("## 引用\n[1] [2]")


def test_semantic_cache_must_be_substantive_and_scoped_to_selected_kb():
    grounded = {
        "answer": "FastAPI 与 PostgreSQL 是项目技术栈。[1]",
        "sources": [{"kb_id": "kb-a", "content": "evidence"}],
    }

    assert chat_router._cache_hit_is_grounded(grounded, "kb-a")
    assert not chat_router._cache_hit_is_grounded({**grounded, "answer": "[1]"}, "kb-a")
    assert not chat_router._cache_hit_is_grounded(grounded, "kb-b")
    assert not chat_router._cache_hit_is_grounded({**grounded, "sources": []}, "kb-a")
