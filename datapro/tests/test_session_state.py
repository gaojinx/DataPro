"""会话状态存储（B11）：Redis 后端 + 内存降级。"""
import pytest

from app.services.session_state import MAX_MESSAGES, SessionState

DEAD_URL = "redis://127.0.0.1:16379/15"      # 无人监听，用于验证降级
REDIS_URL = "redis://127.0.0.1:6379/15"      # 本机 Redis 的独立 db，测试前后清理


async def _redis_available(url: str) -> bool:
    try:
        from redis.asyncio import Redis
        client = Redis.from_url(url, socket_connect_timeout=0.5)
        await client.ping()
        await client.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_memory_fallback_full_flow():
    """Redis 不可用时降级内存，行为与迁移前一致。"""
    store = SessionState(url=DEAD_URL, ttl_seconds=60)
    sid = "s-mem"
    await store.claim_owner(sid, "alice")
    assert await store.owner(sid) == "alice"
    history = await store.add_user_message(sid, "查昨天的支付订单数")
    assert history[-1] == {"role": "user", "content": "查昨天的支付订单数"}
    await store.add_assistant_message(sid, "{}")
    assert len(await store.history(sid)) == 2

    assert await store.bump_clarify(sid) == 1
    assert await store.bump_clarify(sid) == 2
    await store.clear_clarify(sid)
    assert await store.clarify_count(sid) == 0

    await store.set_spec(sid, {"metric": "支付订单数"})
    assert await store.spec(sid) == {"metric": "支付订单数"}

    assert await store.list_sessions("alice") == [{"session_id": sid, "turns": 2}]
    assert await store.list_sessions("bob") == []
    assert await store.list_sessions("bob", is_admin=True) == [{"session_id": sid, "turns": 2}]

    await store.delete(sid)
    assert await store.list_sessions("alice") == []


async def test_history_is_trimmed():
    store = SessionState(url=DEAD_URL, ttl_seconds=60)
    for i in range(30):
        await store.add_user_message("s-trim", f"q{i}")
        await store.add_assistant_message("s-trim", "{}")
    history = await store.history("s-trim")
    assert len(history) == MAX_MESSAGES
    assert history[-1] == {"role": "assistant", "content": "{}"}


async def test_redis_backend_is_shared_across_instances():
    """Redis 后端：另一个进程（新实例）能读到同一会话的历史与槽位。"""
    if not await _redis_available(REDIS_URL):
        pytest.skip("本机未运行 Redis")
    first = SessionState(url=REDIS_URL, ttl_seconds=60)
    await first.delete("s-redis")
    await first.claim_owner("s-redis", "alice")
    await first.add_user_message("s-redis", "查2026-11-20的支付订单数")
    await first.set_spec("s-redis", {"metric": "支付订单数", "grain": "total"})

    second = SessionState(url=REDIS_URL, ttl_seconds=60)   # 模拟另一副本
    assert await second.owner("s-redis") == "alice"
    assert (await second.history("s-redis"))[-1]["content"] == "查2026-11-20的支付订单数"
    assert (await second.spec("s-redis"))["grain"] == "total"
    assert await second.bump_clarify("s-redis") == 1
    assert await second.list_sessions("alice") == [{"session_id": "s-redis", "turns": 1}]
    assert await first.clarify_count("s-redis") == 1
    await first.delete("s-redis")
    assert await second.list_sessions("alice") == []
