"""会话状态存储（B11）：历史消息、会话归属、澄清轮次与多轮槽位。

优先 Redis（多实例共享 + TTL 过期）；连接失败时降级为进程内存并只记一次日志，
保证单机/离线环境仍可用，行为与迁移前一致。
"""
import json
import logging
import time

from app.config import settings

logger = logging.getLogger("datapro.session_state")

INDEX_KEY = "dp:sessions"
MAX_MESSAGES = 40                 # 只保留最近 20 轮，控制 prompt 体积与存储


def _key(session_id: str, part: str = "") -> str:
    return f"dp:sess:{session_id}" + (f":{part}" if part else "")


class SessionState:
    def __init__(self, url: str | None = None, ttl_seconds: int | None = None):
        self._url = url or settings.redis_url
        self._ttl = ttl_seconds or settings.session_ttl_hours * 3600
        self._redis = None
        self._disabled = False
        self._memory: dict[str, dict] = {}

    # ---------- 连接与降级 ----------
    async def _client(self):
        if self._redis is not None or self._disabled:
            return self._redis
        try:
            from redis.asyncio import Redis
            client = Redis.from_url(self._url, decode_responses=True, protocol=2,
                                    socket_connect_timeout=0.5, socket_timeout=2.0)
            await client.ping()
            self._redis = client
            logger.info("会话状态使用 Redis：%s", self._url)
        except Exception as exc:  # noqa: BLE001 —— Redis 故障不能影响主链路
            self._disabled = True
            logger.warning("Redis 不可用（%s），会话状态降级为进程内存", exc)
        return self._redis

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl
        for sid in [s for s, v in self._memory.items() if v["updated"] < cutoff]:
            self._memory.pop(sid, None)

    def _mem(self, session_id: str, create: bool = False) -> dict | None:
        self._prune()
        item = self._memory.get(session_id)
        if item is None and create:
            item = self._memory[session_id] = {"owner": None, "clarify": 0, "spec": None,
                                               "msgs": [], "updated": time.time()}
        return item

    # ---------- 归属 ----------
    async def owner(self, session_id: str) -> str | None:
        r = await self._client()
        if r:
            return await r.hget(_key(session_id), "owner")
        return (self._mem(session_id) or {}).get("owner")

    async def claim_owner(self, session_id: str, username: str) -> bool:
        """原子认领会话；返回 True 表示认领成功，False 表示已被他人占用。"""
        r = await self._client()
        if r:
            async with r.pipeline(transaction=True) as pipe:
                pipe.hsetnx(_key(session_id), "owner", username)
                pipe.expire(_key(session_id), self._ttl)
                pipe.zadd(INDEX_KEY, {session_id: time.time()})
                pipe.zremrangebyscore(INDEX_KEY, "-inf", time.time() - self._ttl)
                result = await pipe.execute()
            return bool(result[0])
        item = self._mem(session_id, create=True)
        if item.get("owner") and item["owner"] != username:
            return False
        item["owner"] = username
        item["updated"] = time.time()
        return True

    # ---------- 历史消息 ----------
    async def history(self, session_id: str) -> list[dict]:
        r = await self._client()
        if r:
            raw = await r.lrange(_key(session_id, "msgs"), 0, -1)
            return [json.loads(x) for x in raw]
        return list((self._mem(session_id) or {}).get("msgs") or [])

    async def _append(self, session_id: str, message: dict) -> list[dict]:
        r = await self._client()
        if r:
            key = _key(session_id, "msgs")
            async with r.pipeline(transaction=True) as pipe:
                pipe.rpush(key, json.dumps(message, ensure_ascii=False, default=str))
                pipe.ltrim(key, -MAX_MESSAGES, -1)
                pipe.expire(key, self._ttl)
                pipe.expire(_key(session_id), self._ttl)
                pipe.zadd(INDEX_KEY, {session_id: time.time()})
                pipe.zremrangebyscore(INDEX_KEY, "-inf", time.time() - self._ttl)
                await pipe.execute()
            raw = await r.lrange(key, 0, -1)
            return [json.loads(x) for x in raw]
        item = self._mem(session_id, create=True)
        item["msgs"].append(message)
        del item["msgs"][:-MAX_MESSAGES]
        item["updated"] = time.time()
        return list(item["msgs"])

    async def add_user_message(self, session_id: str, text: str) -> list[dict]:
        return await self._append(session_id, {"role": "user", "content": text})

    async def add_assistant_message(self, session_id: str, text: str) -> None:
        await self._append(session_id, {"role": "assistant", "content": text})

    # ---------- 澄清轮次 ----------
    async def clarify_count(self, session_id: str) -> int:
        r = await self._client()
        if r:
            return int(await r.hget(_key(session_id), "clarify") or 0)
        return int((self._mem(session_id) or {}).get("clarify") or 0)

    async def bump_clarify(self, session_id: str) -> int:
        r = await self._client()
        if r:
            key = _key(session_id)
            async with r.pipeline(transaction=True) as pipe:
                pipe.hincrby(key, "clarify", 1)
                pipe.expire(key, self._ttl)
                values = await pipe.execute()
            return int(values[0])
        item = self._mem(session_id, create=True)
        item["clarify"] = int(item.get("clarify") or 0) + 1
        item["updated"] = time.time()
        return item["clarify"]

    async def clear_clarify(self, session_id: str) -> None:
        r = await self._client()
        if r:
            await r.hdel(_key(session_id), "clarify")
            return
        item = self._mem(session_id)
        if item:
            item["clarify"] = 0

    # ---------- 多轮槽位 ----------
    async def spec(self, session_id: str) -> dict | None:
        r = await self._client()
        if r:
            raw = await r.hget(_key(session_id), "spec")
            return json.loads(raw) if raw else None
        return (self._mem(session_id) or {}).get("spec")

    async def set_spec(self, session_id: str, spec: dict) -> None:
        r = await self._client()
        if r:
            key = _key(session_id)
            async with r.pipeline(transaction=True) as pipe:
                pipe.hset(key, "spec", json.dumps(spec, ensure_ascii=False))
                pipe.expire(key, self._ttl)
                await pipe.execute()
            return
        item = self._mem(session_id, create=True)
        item["spec"] = spec
        item["updated"] = time.time()

    # ---------- 口径待选择（P1 口径歧义） ----------
    async def pending_caliber(self, session_id: str) -> dict | None:
        r = await self._client()
        if r:
            raw = await r.hget(_key(session_id), "pending_caliber")
            return json.loads(raw) if raw else None
        return (self._mem(session_id) or {}).get("pending_caliber")

    async def set_pending_caliber(self, session_id: str, payload: dict) -> None:
        r = await self._client()
        if r:
            key = _key(session_id)
            async with r.pipeline(transaction=True) as pipe:
                pipe.hset(key, "pending_caliber", json.dumps(payload, ensure_ascii=False))
                pipe.expire(key, self._ttl)
                await pipe.execute()
            return
        item = self._mem(session_id, create=True)
        item["pending_caliber"] = payload
        item["updated"] = time.time()

    async def clear_pending_caliber(self, session_id: str) -> None:
        r = await self._client()
        if r:
            await r.hdel(_key(session_id), "pending_caliber")
            return
        item = self._mem(session_id)
        if item:
            item.pop("pending_caliber", None)

    # ---------- 列表与删除 ----------
    async def list_sessions(self, username: str, is_admin: bool = False,
                            limit: int = 200) -> list[dict]:
        r = await self._client()
        if r:
            await r.zremrangebyscore(INDEX_KEY, "-inf", time.time() - self._ttl)
            ids = await r.zrange(INDEX_KEY, -limit, -1)
            out = []
            async with r.pipeline(transaction=False) as pipe:
                for sid in ids:
                    pipe.hget(_key(sid), "owner")
                    pipe.llen(_key(sid, "msgs"))
                values = await pipe.execute()
            for i, sid in enumerate(ids):
                owner, turns = values[2 * i], int(values[2 * i + 1] or 0)
                if is_admin or owner == username:
                    out.append({"session_id": sid, "turns": turns})
            return out
        self._prune()
        return [{"session_id": sid, "turns": len(item["msgs"])}
                for sid, item in self._memory.items()
                if is_admin or item["owner"] == username]

    async def delete(self, session_id: str) -> None:
        r = await self._client()
        if r:
            await r.delete(_key(session_id), _key(session_id, "msgs"))
            await r.zrem(INDEX_KEY, session_id)
        self._memory.pop(session_id, None)


session_state = SessionState()
