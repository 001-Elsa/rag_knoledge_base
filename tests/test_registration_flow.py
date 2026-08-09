"""Registration must commit independently from login token infrastructure."""

import uuid

import pytest
from starlette.requests import Request

from app.models import User
from app.routers import auth
from app.schemas import LoginRequest, RegisterRequest
from app.security import verify_password


class _Result:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    def __init__(self):
        self.info = {}
        self.objects = []
        self.committed = False

    async def execute(self, _statement, _params=None):
        return _Result()

    def add(self, obj):
        self.objects.append(obj)

    async def flush(self):
        for obj in self.objects:
            if hasattr(obj, "id") and obj.id is None:
                obj.id = uuid.uuid4().hex

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.committed = False

    async def refresh(self, _obj):
        return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/register",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_registration_commits_without_issuing_tokens(monkeypatch):
    async def unavailable_token_service(*_args, **_kwargs):
        raise AssertionError("registration must not call the token service")

    monkeypatch.setattr(auth, "issue_token_pair", unavailable_token_service)
    session = _FakeSession()
    response = await auth.register(
        RegisterRequest(
            username=" new_user ",
            phone="+86 138-0013-8000",
            password="SamePass123!",
        ),
        _request(),
        session,
    )

    assert session.committed is True
    assert response.message == "注册成功，请登录"
    assert response.username == "new_user"
    user = next(item for item in session.objects if isinstance(item, User))
    assert user.phone == "13800138000"
    assert verify_password("SamePass123!", user.password_hash)


def test_login_and_register_normalize_username_the_same_way():
    registration = RegisterRequest(
        username="  Alice  ", phone="0086 139-0013-9000", password="abcdef"
    )
    assert registration.username == "Alice"
    assert registration.phone == "13900139000"
    assert LoginRequest(username="  Alice  ", password="abcdef").username == "Alice"


def test_phone_has_a_database_unique_constraint():
    constraints = {constraint.name for constraint in User.__table__.constraints}
    assert "uq_users_phone" in constraints


def test_frontend_switches_to_login_after_registration():
    page = open("app/static/index.html", encoding="utf-8").read()
    register_branch = page.index("if (this.authTab === 'register')")
    token_write = page.index("localStorage.setItem('rag_access_token'", register_branch)
    switch_to_login = page.index("this.authTab = 'login'", register_branch)
    success_message = page.index("注册成功，请登录", register_branch)
    assert switch_to_login < token_write
    assert success_message < token_write
    assert 'v-if="authTab===\'register\'"' in page
    assert "用户中心" in page


def test_password_work_is_offloaded_from_the_async_event_loop():
    source = open("app/routers/auth.py", encoding="utf-8").read()
    assert "await asyncio.to_thread(hash_password, body.password)" in source
    assert "await asyncio.to_thread(\n        verify_password" in source
