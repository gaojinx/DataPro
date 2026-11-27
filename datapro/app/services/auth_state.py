"""账号实时状态：管理员/用户权限隔离的权威源。

设计要点：
- Token 只携带身份声明（uid/sub/jti/exp/epoch），role/status/permissions 一律回查 user_account。
- 短 TTL 缓存降低 DB 压力；改密/禁用/改角色/改权限时 bump_version 递增版本号并主动清缓存，
  使旧令牌在下一次请求立即失效，不依赖缓存自然过期。
- DB 不可用时 load() 抛异常，由调用方 fail-closed（拒绝鉴权），不降级为旧 Token 快照。
"""
import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("datapro.auth_state")

CACHE_TTL_SECONDS = 3.0

_cache: dict[str, tuple[float, "AccountState"]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class AccountState:
    id: int
    username: str
    display_name: str
    role: str
    status: str
    permissions: str | None
    token_version: int

    @property
    def active(self) -> bool:
        return self.status == "active"


def invalidate(username: str) -> None:
    """主动清除缓存：权限变更必须即时生效。"""
    with _lock:
        _cache.pop(username, None)


def load(username: str, *, refresh: bool = False) -> AccountState | None:
    """读取账号实时状态；None 表示账号不存在。"""
    now = time.monotonic()
    if not refresh:
        with _lock:
            hit = _cache.get(username)
        if hit is not None and hit[0] > now:
            return hit[1]

    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.query(User).filter(User.username == username).first()
        if row is None:
            with _lock:
                _cache.pop(username, None)
            return None
        state = AccountState(
            id=row.id, username=row.username, display_name=row.display_name,
            role=row.role, status=row.status, permissions=row.permissions,
            token_version=int(row.token_version or 0),
        )
    with _lock:
        _cache[username] = (now + CACHE_TTL_SECONDS, state)
    return state


def token_version(username: str) -> int:
    """签发令牌时取最新版本号；账号不存在返回 -1。"""
    state = load(username, refresh=True)
    return state.token_version if state else -1


def bump_version(username: str) -> int:
    """原子递增令牌版本并使缓存失效；账号不存在返回 -1。"""
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.query(User).filter(User.username == username).first()
        if row is None:
            return -1
        row.token_version = int(row.token_version or 0) + 1
        version = row.token_version
        db.commit()
    invalidate(username)
    logger.info("账号 %s 令牌版本递增至 %s", username, version)
    return version
