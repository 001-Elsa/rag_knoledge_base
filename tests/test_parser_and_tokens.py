"""解析器分段与 Refresh Token 白名单逻辑测试（Redis 用假实现替身）。"""
import asyncio

import pytest

from app.services import tokens as tokens_svc
from app.services.parser import parse_file


def test_parse_txt_returns_single_segment(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("你好，世界", encoding="utf-8")
    segments = parse_file(str(f))
    assert segments == [("你好，世界", None)]


def test_parse_unsupported_type(tmp_path):
    f = tmp_path / "a.xyz"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(str(f))


class FakeRedis:
    """内存版 Redis 替身，覆盖 token family 所需命令。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.sets.pop(key, None)

    async def exists(self, key):
        return int(key in self.store or key in self.sets)

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)

    async def smembers(self, key):
        return self.sets.get(key, set()).copy()

    async def expire(self, key, ttl):
        return True

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __getattr__(self, name):
        def queue(*args):
            self.commands.append((name, args))
            return self

        return queue

    async def execute(self):
        for name, args in self.commands:
            await getattr(self.redis, name)(*args)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(tokens_svc, "get_redis", lambda: fake)
    return fake


def test_refresh_rotation_invalidates_old_token(fake_redis):
    async def scenario():
        _, refresh1 = await tokens_svc.issue_token_pair("user-1")
        # 第一次轮换成功
        pair = await tokens_svc.rotate_refresh_token(refresh1)
        assert pair is not None
        # 旧 token 已作废，重放失败
        assert await tokens_svc.rotate_refresh_token(refresh1) is None
        # 旧 token 重放会吊销整个 family，新 token 也必须重新登录
        _, refresh2 = pair
        assert await tokens_svc.rotate_refresh_token(refresh2) is None

    asyncio.run(scenario())


def test_revoked_refresh_cannot_rotate(fake_redis):
    async def scenario():
        _, refresh = await tokens_svc.issue_token_pair("user-1")
        await tokens_svc.revoke_refresh_token(refresh)  # 登出
        assert await tokens_svc.rotate_refresh_token(refresh) is None

    asyncio.run(scenario())


def test_garbage_refresh_token(fake_redis):
    async def scenario():
        assert await tokens_svc.rotate_refresh_token("garbage") is None
        await tokens_svc.revoke_refresh_token("garbage")  # 幂等，不抛异常

    asyncio.run(scenario())


def test_password_change_can_revoke_all_user_families(fake_redis):
    async def scenario():
        _, refresh_a = await tokens_svc.issue_token_pair("user-1")
        _, refresh_b = await tokens_svc.issue_token_pair("user-1")
        await tokens_svc.revoke_all_user_families("user-1")
        assert await tokens_svc.rotate_refresh_token(refresh_a) is None
        assert await tokens_svc.rotate_refresh_token(refresh_b) is None

    asyncio.run(scenario())
