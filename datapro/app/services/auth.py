"""认证服务（B12 基础版）：PBKDF2 密码哈希、HMAC 签名 Token、角色鉴权依赖。"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger("datapro.auth")

_ITERATIONS = 120_000
TOKEN_SEP = "."

# 登录密码与角色在启动时初始化；生产应由管理员改密或对接企业 SSO
SEED_USERS = (
    ("admin", "管理员", "admin", settings.auth_seed_admin_password),
    ("analyst", "数据分析师", "user", settings.auth_seed_user_password),
)


class AuthUser(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    permissions: list[str] = []      # 生效权限（管理员恒为全量）；旧 Token 缺省时按角色默认补齐

    def can(self, code: str) -> bool:
        return self.role == "admin" or code in self.permissions


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64e(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())


def create_token(user: AuthUser, ttl_hours: int | None = None, epoch: int = 0) -> str:
    """签发令牌：只携带身份声明与版本，不含 role/perm 授权字段。

    角色与权限在每次请求时从 user_account 实时读取（见 _user_from_token），
    避免旧令牌快照绕过降权/禁用/改权限。name 仅用于展示，不参与授权。
    """
    ttl = settings.auth_token_ttl_hours if ttl_hours is None else ttl_hours
    payload = {
        "uid": user.id,
        "sub": user.username,
        "name": user.display_name,        # 仅展示，不参与授权
        "exp": int(time.time()) + ttl * 3600,
        "iat": int(time.time()),
        "jti": secrets.token_hex(8),      # 单令牌撤销（退出登录）
        "epoch": epoch,                   # 账号级令牌版本（改密/禁用时整体失效）
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}{TOKEN_SEP}{_sign(body)}"


def decode_token(token: str) -> dict | None:
    """校验签名与有效期；任何异常都视为无效 Token。"""
    try:
        body, signature = token.split(TOKEN_SEP)
        if not hmac.compare_digest(signature, _sign(body)):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def seed_users() -> None:
    """首次启动写入演示账号；已存在则跳过（不覆盖已改密码）。"""
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        created = []
        for username, display_name, role, password in SEED_USERS:
            if db.query(User).filter(User.username == username).first():
                continue
            db.add(User(username=username, display_name=display_name, role=role,
                        password_hash=hash_password(password)))
            created.append(username)
        if created:
            db.commit()
            logger.info("已初始化登录账号：%s", ", ".join(created))


def authenticate(username: str, password: str) -> AuthUser | None:
    from app.db.models import User
    from app.db.session import SessionLocal
    from app.services.permissions import matrix

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username.strip()).first()
        if not user or user.status != "active" or not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.now()
        db.commit()
        auth = AuthUser(id=user.id, username=user.username,
                        display_name=user.display_name, role=user.role,
                        permissions=matrix.resolve(user.role, user.permissions))
        # 令牌版本随 AuthUser 透传，避免二次查库；不进 Token 的授权字段
        auth.__dict__["_token_version"] = int(user.token_version or 0)
        return auth


async def _user_from_token(raw: str | None) -> AuthUser:
    """鉴权权威源：DB 实时状态 + 令牌版本校验 + 撤销黑名单。

    旧令牌（含 role/perm 字段）依然可按身份解析，但角色与权限一律以 DB 为准，
    因此降权/禁用后即使令牌未过期也不会再获得旧权限。
    """
    import asyncio

    payload = decode_token(raw or "")
    if not payload:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    from app.services import auth_state
    from app.services.permissions import matrix
    from app.services.token_store import token_store

    if await token_store.is_revoked(payload.get("jti")):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    username = payload.get("sub") or ""
    try:
        state = await asyncio.to_thread(auth_state.load, username)
    except Exception as exc:  # noqa: BLE001 —— DB 不可用时 fail-closed，不放行旧权限快照
        logger.warning("鉴权时读取账号状态失败，拒绝请求：%s", exc)
        raise HTTPException(status_code=503, detail="账号状态校验暂不可用") from exc
    if state is None or not state.active:
        raise HTTPException(status_code=401, detail="账号不存在或已禁用，请重新登录")
    if int(payload.get("epoch", 0)) != state.token_version:
        raise HTTPException(status_code=401, detail="账号信息已变更，请重新登录")

    return AuthUser(id=state.id, username=state.username,
                    display_name=state.display_name, role=state.role,
                    permissions=matrix.resolve(state.role, state.permissions))


async def revoke_token(raw: str | None) -> bool:
    """退出登录：把当前令牌加入黑名单，TTL 到原过期时间。"""
    payload = decode_token(raw or "")
    if not payload or not payload.get("jti"):
        return False
    from app.services.token_store import token_store
    await token_store.revoke(payload["jti"], int(payload.get("exp", 0)) - int(time.time()))
    return True


async def get_current_user(authorization: str | None = Header(default=None)) -> AuthUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return await _user_from_token(authorization.split(" ", 1)[1].strip())


async def get_current_user_flexible(authorization: str | None = Header(default=None),
                                    token: str | None = None) -> AuthUser:
    """文件预览/下载依赖浏览器原生请求（a/img/iframe），无法带 Header，支持 ?token=。"""
    if authorization and authorization.lower().startswith("bearer "):
        return await _user_from_token(authorization.split(" ", 1)[1].strip())
    return await _user_from_token(token)


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


def require_permission(code: str):
    """细粒度授权依赖（B12 权限矩阵）：管理员恒通过，其余按生效权限判断。"""
    async def checker(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if not user.can(code):
            raise HTTPException(status_code=403, detail=f"缺少权限：{code}")
        return user
    return checker
