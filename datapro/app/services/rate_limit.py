"""请求限流（B14 / E007）：Redis 固定窗口计数，Redis 不可用时降级进程内存。

按用户 + 分钟计数，超过 `RATE_LIMIT_PER_MINUTE` 则拒绝；配置为 0 关闭限流。
限流是旁路能力，任何存储异常都只记日志，不阻断主链路。
"""
import logging
import time

from app.config import settings

logger = logging.getLogger("datapro.rate_limit")

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, url: str | None = None, limit: int | None = None):
        self._url = url or settings.redis_url
        self._limit = limit
        self._redis = None
        self._disabled = False
        self._memory: dict[str, tuple[int, float]] = {}

    @property
    def limit(self) -> int:
        return settings.rate_limit_per_minute if self._limit is None else self._limit

    async def _client(self):
        if self._redis is not None or self._disabled:
            return self._redis
        try:
            from redis.asyncio import Redis
            client = Redis.from_url(self._url, decode_responses=True, protocol=2,
                                    socket_connect_timeout=0.5, socket_timeout=2.0)
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001 —— 限流存储故障不能影响主链路
            self._disabled = True
            logger.warning("Redis 不可用（%s），限流降级为进程内存计数", exc)
        return self._redis

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, (_, expire_at) in self._memory.items() if expire_at <= now]:
            self._memory.pop(key, None)

    async def hit(self, key: str) -> int | None:
        """记一次请求：放行返回 None；超限返回建议等待秒数。"""
        limit = self.limit
        if limit <= 0:
            return None
        bucket = int(time.time() // WINDOW_SECONDS)
        redis_key = f"dp:rl:{key}:{bucket}"
        wait = WINDOW_SECONDS - int(time.time() % WINDOW_SECONDS)

        client = await self._client()
        if client:
            try:
                async with client.pipeline(transaction=True) as pipe:
                    pipe.incr(redis_key)
                    pipe.expire(redis_key, WINDOW_SECONDS + 5)
                    count, _ = await pipe.execute()
                return wait if int(count) > limit else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("限流 Redis 异常，降级内存计数：%s", exc)

        self._prune()
        count, expire_at = self._memory.get(redis_key, (0, time.time() + wait))
        count += 1
        self._memory[redis_key] = (count, expire_at)
        return max(1, int(expire_at - time.time())) if count > limit else None


rate_limiter = RateLimiter()
