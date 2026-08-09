"""Fast regression checks for tenant identity and distributed capacity controls."""

import asyncio

import pytest
from starlette.requests import Request

from app.config import settings
from app.db import set_tenant_context
from app.limiter import authenticated_principal_or_ip
from app.services import concurrency
from app.services.tenancy import lock_workspace_quota


def _request(token: str = "", client: str = "10.0.0.8") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat",
            "headers": headers,
            "client": (client, 12345),
        }
    )


def test_rate_limit_identity_separates_users_behind_one_ip():
    first = authenticated_principal_or_ip(_request("user-a"))
    second = authenticated_principal_or_ip(_request("user-b"))
    assert first != second
    assert first == authenticated_principal_or_ip(_request("user-a"))
    assert authenticated_principal_or_ip(_request()) == "ip:10.0.0.8"
    assert "user-a" not in first


@pytest.mark.asyncio
async def test_capacity_allows_users_in_parallel_but_serializes_one_conversation(monkeypatch):
    def redis_unavailable():
        raise ConnectionError("offline")

    monkeypatch.setattr(concurrency, "get_redis", redis_unavailable)
    monkeypatch.setattr(settings, "chat_global_concurrency", 2)
    monkeypatch.setattr(settings, "chat_user_concurrency", 2)
    concurrency.reset_local_slots_for_tests()

    first = await concurrency.acquire_chat_slot("user-a", "conversation-1")
    assert first is not None
    assert await concurrency.acquire_chat_slot("user-a", "conversation-1") is None

    second = await concurrency.acquire_chat_slot("user-a", "conversation-2")
    assert second is not None
    assert await concurrency.acquire_chat_slot("user-b", "conversation-3") is None

    await concurrency.release_chat_slot(first)
    third = await concurrency.acquire_chat_slot("user-b", "conversation-3")
    assert third is not None
    await asyncio.gather(
        concurrency.release_chat_slot(second),
        concurrency.release_chat_slot(third),
    )
    concurrency.reset_local_slots_for_tests()


class _RecordingSession:
    def __init__(self):
        self.info = {}
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))


@pytest.mark.asyncio
async def test_tenant_context_is_transaction_local_and_remembered_for_next_commit():
    session = _RecordingSession()
    await set_tenant_context(session, "user-123")
    assert session.info["rls_user_id"] == "user-123"
    assert "set_config('app.user_id'" in session.calls[0][0]
    assert ", true)" in session.calls[0][0]
    assert session.calls[0][1] == {"user_id": "user-123"}


@pytest.mark.asyncio
async def test_workspace_quota_uses_transaction_advisory_lock():
    session = _RecordingSession()
    await lock_workspace_quota(session, "workspace-7")
    assert "pg_advisory_xact_lock" in session.calls[0][0]
    assert session.calls[0][1]["lock_key"] == "workspace-storage:workspace-7"
