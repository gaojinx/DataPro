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


def create_token(user: AuthUser, ttl_hours: int | None = None) -> str:
    ttl = settings.auth_token_ttl_hours if ttl_hours is None else ttl_hours
    payload = {
        "uid": user.id,
        "sub": user.username,
        "name": user.display_name,
        "role": user.role,
        "exp": int(time.time()) + ttl * 3600,
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

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username.strip()).first()
        if not user or user.status != "active" or not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.now()
        db.commit()
        return AuthUser(id=user.id, username=user.username,
                        display_name=user.display_name, role=user.role)


def get_current_user(authorization: str | None = Header(default=None)) -> AuthUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return AuthUser(id=int(payload["uid"]), username=payload["sub"],
                    display_name=payload.get("name", payload["sub"]), role=payload.get("role", "user"))


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user
