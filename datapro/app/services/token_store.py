"""Token 撤销与账号令牌版本（B12）：撤销走 Redis，令牌版本以数据库为准。

- `revoke(jti, ttl)`：退出登录时把令牌加入黑名单，直到原有效期结束
- `epoch/bump_epoch`：改密、禁用、改角色时递增 user_account.token_version，
  权威源在数据库（跨实例一致、重启不丢失），Redis/内存仅作撤销黑名单
"""
import asyncio
import logging
import time

from app.config import settings

logger = logging.getLogger("datapro.token_store")

REVOKED_PREFIX = "dp:auth:revoked:"
EPOCH_PREFIX = "dp:auth:epoch:"


class TokenStore:
    def __init__(self, url: str | None = None):
        self._url = url or settings.redis_url
        self._redis = None
        self._disabled = False
        self._revoked: dict[str, float] = {}
        self._epochs: dict[str, int] = {}

    async def _client(self):
        if self._redis is not None or self._disabled:
            return self._redis
        try:
            from redis.asyncio import Redis
            client = Redis.from_url(self._url, decode_responses=True, protocol=2,
                                    socket_connect_timeout=0.5, socket_timeout=2.0)
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001 —— 认证存储故障不能影响主链路
            self._disabled = True
            logger.warning("Redis 不可用（%s），Token 黑名单降级为进程内存", exc)
        return self._redis

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        """撤销单个令牌；TTL 到原 Token 过期时间即可，不必永久保留。"""
        if not jti:
            return
        ttl = max(1, int(ttl_seconds))
        client = await self._client()
        if client:
            try:
                await client.set(REVOKED_PREFIX + jti, "1", ex=ttl)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("撤销令牌写 Redis 失败，降级内存：%s", exc)
        self._revoked[jti] = time.time() + ttl

    async def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        client = await self._client()
        if client:
            try:
                return bool(await client.exists(REVOKED_PREFIX + jti))
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询令牌黑名单失败，降级内存：%s", exc)
        expire_at = self._revoked.get(jti)
        if expire_at is None:
            return False
        if expire_at <= time.time():
            self._revoked.pop(jti, None)
            return False
        return True

    @staticmethod
    async def epoch(username: str) -> int:
        """令牌版本权威源：user_account.token_version（DB）。"""
        from app.services import auth_state

        try:
            return await asyncio.to_thread(auth_state.token_version, username)
        except Exception as exc:  # noqa: BLE001 —— DB 不可用时 fail-closed（负数不匹配任何令牌）
            logger.warning("读取账号令牌版本失败，按失效处理：%s", exc)
            return -1

    @staticmethod
    async def bump_epoch(username: str) -> int:
        """递增账号令牌版本，使已签发令牌立即全部失效；写库失败时抛异常阻断变更。"""
        from app.services import auth_state

        version = await asyncio.to_thread(auth_state.bump_version, username)
        if version < 0:
            raise LookupError(f"账号 {username} 不存在，无法递增令牌版本")
        return version


token_store = TokenStore()
