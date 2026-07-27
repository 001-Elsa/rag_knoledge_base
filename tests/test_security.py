"""认证模块单元测试。"""
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_hash_and_verify():
    hashed = hash_password("my-secret-123")
    assert hashed != "my-secret-123"
    assert verify_password("my-secret-123", hashed)
    assert not verify_password("wrong-password", hashed)


def test_same_password_different_hash():
    """bcrypt 每次加盐，同一密码两次哈希结果应不同。"""
    assert hash_password("abc123456") != hash_password("abc123456")


def test_jwt_roundtrip():
    token = create_access_token("user-42")
    assert decode_access_token(token) == "user-42"


def test_jwt_invalid_token():
    assert decode_access_token("not-a-real-token") is None
    assert decode_access_token("") is None


def test_refresh_token_cannot_be_used_as_access():
    """类型混淆防护：Refresh Token 不能当 Access Token 用（反之亦然）。"""
    refresh = create_refresh_token("user-42", jti="jti-1")
    assert decode_access_token(refresh) is None
    access = create_access_token("user-42")
    assert decode_token(access, "refresh") is None


def test_refresh_token_carries_jti():
    refresh = create_refresh_token("user-42", jti="jti-abc")
    payload = decode_token(refresh, "refresh")
    assert payload is not None
    assert payload["jti"] == "jti-abc"
    assert payload["sub"] == "user-42"
