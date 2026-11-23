"""认证基础能力单测：密码哈希、Token 签名与有效期、角色校验。"""
import pytest
from fastapi import HTTPException

from app.services.auth import (AuthUser, create_token, decode_token, hash_password,
                               require_admin, verify_password)

USER = AuthUser(id=1, username="admin", display_name="管理员", role="admin")


def test_password_hash_roundtrip():
    stored = hash_password("secret-123")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("secret-123", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("secret-123", "broken")


def test_token_roundtrip():
    token = create_token(USER)
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload["role"] == "admin"


def test_token_tampered_rejected():
    token = create_token(USER)
    body, signature = token.split(".")
    assert decode_token(f"{body}.{signature[:-2]}xx") is None
    assert decode_token("not-a-token") is None


def test_token_expired_rejected():
    assert decode_token(create_token(USER, ttl_hours=-1)) is None


def test_require_admin():
    assert require_admin(USER).username == "admin"
    normal = AuthUser(id=2, username="analyst", display_name="分析师", role="user")
    with pytest.raises(HTTPException) as exc:
        require_admin(normal)
    assert exc.value.status_code == 403
