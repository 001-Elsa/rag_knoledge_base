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
    """内存版 Redis 替身：只实现 tokens.py 用到的三个命令。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def delete(self, key):
        self.store.pop(key, None)


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
        # 新 token 可以继续用
        _, refresh2 = pair
        assert await tokens_svc.rotate_refresh_token(refresh2) is not None

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
