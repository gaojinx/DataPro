"""限定范围缓存（U-B03）：只缓存意图分类与口径问答，绝不缓存取数结果。

为什么只限这两个面
- 取数结果与「谁在看、能看哪些表、分区到哪天、裁决过哪些口径」强相关，键少任何一个维度
  就是越权串味；收益远小于风险，所以一律不进缓存。
- 意图分类是「问题 + 最近几轮提问」的纯函数；口径问答是「问题 + 命中资料」的纯函数。
  把资料指纹放进键，知识库/口径一改键自然失效，不需要额外的失效通知（也就没有通知漏发的坑）。

越权串味防线（键的组成）
- 两个域都含：域 + 归一化问题 + 资料指纹
- 口径问答额外含：user_id + role + 可见表白名单指纹（同问题不同用户的可答范围不同）
- 归一化只去空白/大小写/句末标点：这些不改变意图与口径，去掉能提高命中率

存储：Redis（多实例一致）→ 不可用时降级进程内存（单实例/开发）。
`LLM_MOCK=true` 时整体跳过：mock 是确定性假数据，缓存只会让测试互相污染且毫无收益。
"""
import hashlib
import logging
import threading
import time

from app.config import settings

logger = logging.getLogger("datapro.cache")

SCOPE_INTENT = "intent"
SCOPE_META = "meta"
SCOPES = (SCOPE_INTENT, SCOPE_META)
_KEY_PREFIX = "dp:qc"
_MAX_MEMORY = 512


def fingerprint(*parts: object) -> str:
    """把若干标识拼成一个定长短串。

    每段带长度前缀（`len:content`）：只用分隔符的话，内容里带同一个分隔符就能撞键，
    而在多用户共用一个缓存键的场景里，撞键等于串味，必须从编码上排除。
    """
    raw = "".join(f"{len(text)}:{text}" for text in
                  ("" if p is None else str(p) for p in parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize(question: str) -> str:
    """问题归一化：压缩空白、转小写、去掉句末标点。"""
    text = " ".join((question or "").split()).lower()
    return text.rstrip("？?。.!！~～,，")


class QueryCache:
    def __init__(self, url: str | None = None, enabled: bool | None = None):
        self._url = url or settings.redis_url
        self._enabled = enabled
        self._redis = None
        self._disabled = False
        self._memory: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._errors = 0

    # ---------------- 开关与状态 ----------------
    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        # mock 模式跳过：确定性假数据，缓存没有收益却会让测试互相污染
        return settings.query_cache_enabled and not settings.llm_mock

    def _scoped(self, scope: str, key: str) -> str:
        if scope not in SCOPES:
            raise ValueError(f"未知缓存域：{scope}")
        return f"{_KEY_PREFIX}:{scope}:{key}"

    async def _client(self):
        if self._redis is not None or self._disabled:
            return self._redis
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(self._url, decode_responses=True, protocol=2,
                                    socket_connect_timeout=0.5, socket_timeout=1.5)
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001 —— 缓存是旁路能力，故障不能影响主链路
            self._disabled = True
            logger.warning("Redis 不可用（%s），缓存降级为进程内存", exc)
        return self._redis

    # ---------------- 读写 ----------------
    async def get(self, scope: str, key: str) -> dict | None:
        if not self.enabled:
            return None
        full = self._scoped(scope, key)

        client = await self._client()
        if client is not None:
            try:
                import json

                raw = await client.get(full)
                if raw:
                    self._hits += 1
                    return json.loads(raw)
                self._misses += 1
                return None
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.warning("读取缓存失败，按未命中处理：%s", exc)
                return None

        with self._lock:
            self._prune_locked()
            hit = self._memory.get(full)
            if hit and hit[1] > time.time():
                self._hits += 1
                return hit[0]
            self._memory.pop(full, None)
            self._misses += 1
            return None

    async def set(self, scope: str, key: str, value: dict, ttl: int) -> None:
        if not self.enabled or ttl <= 0:
            return
        full = self._scoped(scope, key)

        client = await self._client()
        if client is not None:
            try:
                import json

                await client.set(full, json.dumps(value, ensure_ascii=False), ex=ttl)
                self._sets += 1
                return
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.warning("写入缓存失败，已忽略：%s", exc)
                return

        with self._lock:
            self._prune_locked()
            if len(self._memory) >= _MAX_MEMORY:
                # 内存上限保护：淘汰最早到期的条目，不做 LRU（键少、TTL 短，够用）
                oldest = min(self._memory, key=lambda k: self._memory[k][1])
                self._memory.pop(oldest, None)
            self._memory[full] = (value, time.time() + ttl)
            self._sets += 1

    def _prune_locked(self) -> None:
        now = time.time()
        for k in [k for k, (_, expire) in self._memory.items() if expire <= now]:
            self._memory.pop(k, None)

    async def clear(self, scope: str | None = None) -> int:
        """清空（可按域）：用于口径/知识库更新后的人工兜底，日常无需调用。"""
        removed = 0
        client = await self._client()
        if client is not None:
            patterns = [self._scoped(s, "*") for s in ((scope,) if scope else SCOPES)]
            try:
                for pattern in patterns:
                    async for k in client.scan_iter(match=pattern, count=200):
                        await client.delete(k)
                        removed += 1
                self._hits = self._misses = self._sets = 0
                return removed
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.warning("清空缓存失败：%s", exc)
                return 0
        with self._lock:
            prefix = f"{_KEY_PREFIX}:{scope}:" if scope else f"{_KEY_PREFIX}:"
            for k in [k for k in self._memory if k.startswith(prefix)]:
                self._memory.pop(k, None)
                removed += 1
            self._hits = self._misses = self._sets = 0
        return removed

    def stats(self) -> dict:
        total = self._hits + self._misses
        with self._lock:
            entries = len(self._memory)
        return {
            "enabled": self.enabled,
            "backend": "redis" if self._redis is not None else "memory",
            "scopes": list(SCOPES),
            "hits": self._hits,
            "misses": self._misses,
            "sets": self._sets,
            "errors": self._errors,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
            "memory_entries": entries,
            "ttl": {"intent": settings.cache_ttl_intent, "meta": settings.cache_ttl_meta},
        }


query_cache = QueryCache()
