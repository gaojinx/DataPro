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
    # 授权字段不再进 Token：降权/禁用后旧令牌不能继续携带旧权限
    assert "role" not in payload and "perm" not in payload


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


def test_token_carries_jti_and_epoch():
    payload = decode_token(create_token(USER, epoch=3))
    assert payload["jti"] and payload["epoch"] == 3


async def test_token_store_revocation_memory_fallback():
    """Redis 不可用时撤销黑名单降级内存；令牌版本以 DB 为准。"""
    from app.services import auth_state
    from app.services.token_store import TokenStore

    store = TokenStore(url="redis://127.0.0.1:16379/15")
    assert await store.is_revoked("j-1") is False
    await store.revoke("j-1", 60)
    assert await store.is_revoked("j-1") is True
    versions = {"alice": 0}

    def fake_version(name):
        return versions.get(name, -1)

    def fake_bump(name):
        versions[name] = versions.get(name, 0) + 1
        return versions[name]

    original_version, original_bump = auth_state.token_version, auth_state.bump_version
    auth_state.token_version, auth_state.bump_version = fake_version, fake_bump
    try:
        assert await store.epoch("alice") == 0
        assert await store.bump_epoch("alice") == 1
        assert await store.epoch("alice") == 1
    finally:
        auth_state.token_version, auth_state.bump_version = original_version, original_bump


async def test_downgraded_token_loses_admin(monkeypatch):
    """管理员降权后，旧令牌即使未过期也只拥有 DB 中的新权限。"""
    from app.services import auth_state
    from app.services.auth import _user_from_token, create_token

    token = create_token(USER, epoch=0)
    state = auth_state.AccountState(
        id=1, username="admin", display_name="管理员",
        role="user", status="active", permissions='["chat.use"]', token_version=0,
    )
    monkeypatch.setattr(auth_state, "load", lambda username: state)
    monkeypatch.setattr("app.services.token_store.token_store.is_revoked",
                        lambda jti: _async_false())

    user = await _user_from_token(token)
    assert user.role == "user"
    assert user.permissions == ["chat.use"]
    assert not user.can("users.manage")


async def test_disabled_account_token_rejected(monkeypatch):
    """账号被禁用后旧令牌立即失效。"""
    from app.services import auth_state
    from app.services.auth import _user_from_token, create_token

    token = create_token(USER, epoch=0)
    state = auth_state.AccountState(
        id=1, username="admin", display_name="管理员",
        role="admin", status="disabled", permissions=None, token_version=0,
    )
    monkeypatch.setattr(auth_state, "load", lambda username: state)
    monkeypatch.setattr("app.services.token_store.token_store.is_revoked",
                        lambda jti: _async_false())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await _user_from_token(token)
    assert exc.value.status_code == 401


async def _async_false():
    return False
