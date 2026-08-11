"""The UI and API expose the actual RAG implementation stack."""

import inspect
from pathlib import Path

import pytest

from app.routers import system


@pytest.mark.asyncio
async def test_capabilities_describe_the_real_rag_pipeline():
    payload = await system.capabilities(None)
    technologies = " ".join(item["technology"] for item in payload["stack"])
    assert "FastAPI" in technologies
    assert "PostgreSQL + pgvector" in technologies
    assert "Redis + Celery" in technologies
    assert "混合检索" in payload["retrieval"]
    assert "引用来源" in payload["generation"]
    assert "Agent 跨对话记忆" in payload["generation"]
    assert "Workspace RLS" in payload["security"]


def test_chat_audit_is_scoped_to_a_workspace_and_errors_are_safe():
    chat_source = inspect.getsource(__import__("app.routers.chat", fromlist=["chat"]).chat)
    assert "workspace_id=selected_kb.workspace_id" in chat_source
    assert "resource_id=effective_kb_id" in chat_source

    page = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "const raw = await res.text()" in page
    assert "JSON.parse(raw)" in page
    assert "系统能力与技术栈" in page
