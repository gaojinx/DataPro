"""限定范围缓存（U-B03）：只缓存意图分类与口径问答，取数结果永不进缓存。

验证重点不是「能存能取」，而是**串味防线**：资料变、用户变、权限变，键必须跟着变。
"""
import asyncio
import uuid

import pytest

from app.config import settings
from app.services.query_cache import QueryCache, fingerprint, normalize


def _mem_cache() -> QueryCache:
    """内存后端实例：不依赖本机 Redis，避免测试受环境影响。"""
    cache = QueryCache(enabled=True)

    async def _no_client():
        return None

    cache._client = _no_client          # type: ignore[method-assign]
    return cache


def test_normalize_and_fingerprint_are_stable():
    assert normalize("  最近7天  支付订单数？ ") == "最近7天 支付订单数"
    assert normalize("最近7天 支付订单数") == normalize("最近7天  支付订单数。")
    assert fingerprint("a", "b") == fingerprint("a", "b")
    # 分隔符避免 a|b + c 与 a + b|c 撞键
    assert fingerprint("a\x1fb", "c") != fingerprint("a", "b\x1fc")


def test_memory_backend_set_get_expire_and_clear():
    cache = _mem_cache()

    async def main():
        await cache.set("intent", "k", {"intent": "query"}, 60)
        assert await cache.get("intent", "k") == {"intent": "query"}
        # TTL=0 视为不写
        await cache.set("intent", "k2", {"intent": "meta_qa"}, 0)
        assert await cache.get("intent", "k2") is None
        await cache.set("intent", "k3", {"intent": "query"}, -1)
        assert await cache.get("intent", "k3") is None
        assert await cache.clear("meta") == 0
        assert await cache.clear() >= 1
        assert await cache.get("intent", "k") is None

    asyncio.run(main())


def test_unknown_scope_is_rejected():
    cache = _mem_cache()
    with pytest.raises(ValueError):
        asyncio.run(cache.get("datasource", "k"))


def test_disabled_cache_never_reads_or_writes():
    cache = QueryCache(enabled=False)

    async def main():
        await cache.set("intent", "k", {"intent": "query"}, 60)
        assert await cache.get("intent", "k") is None
        assert cache.stats()["sets"] == 0

    asyncio.run(main())


def test_intent_classification_is_cached(monkeypatch):
    """同一问题连问两次：第二次不应再打模型。"""
    from app.services import intent_llm

    monkeypatch.setattr(settings, "llm_mock", False)
    monkeypatch.setattr(settings, "intent_llm_enabled", True)
    calls = {"n": 0}

    async def fake_chat(prompt, system=""):
        calls["n"] += 1
        return '{"intent":"query","reason":"要取数"}'

    monkeypatch.setattr(intent_llm.llm_client, "chat", fake_chat)
    question = f"缓存验证-{uuid.uuid4().hex} 最近7天支付订单数"

    async def main():
        first = await intent_llm.classify(question)
        second = await intent_llm.classify(question)
        return first, second

    first, second = asyncio.run(main())
    assert first == second == "query"
    assert calls["n"] == 1, "第二次应命中缓存，不应再调用模型"


def test_meta_answer_key_differs_by_visible_tables(monkeypatch):
    """同一问题、不同可见表白名单：必须各自调用模型，不能复用别人的答案。"""
    from app.services import intent as intent_mod
    from app.core.statemachine import PipelineContext

    monkeypatch.setattr(settings, "llm_mock", False)
    calls = {"n": 0}

    async def fake_chat(prompt, system=""):
        calls["n"] += 1
        return f"回答第 {calls['n']} 次"

    monkeypatch.setattr(intent_mod.llm_client, "chat", fake_chat)

    class Recall:
        knowledge = [{"term": "支付订单数", "definition": "已支付订单数量", "formula": "COUNT(*)"}]
        calibers = []
        tables = []

    question = f"缓存验证-{uuid.uuid4().hex} 支付订单数的口径是什么"

    async def main():
        ctx_a = PipelineContext(question=question, user_id="u_a", user_role="user")
        ctx_b = PipelineContext(question=question, user_id="u_b", user_role="user")
        monkeypatch.setattr(intent_mod, "_visible_tables", lambda _u: "t1,t2")
        a1 = await intent_mod.answer_service.answer_meta(ctx_a, Recall())
        a2 = await intent_mod.answer_service.answer_meta(ctx_a, Recall())   # 同用户 → 命中
        monkeypatch.setattr(intent_mod, "_visible_tables", lambda _u: "t1")
        b1 = await intent_mod.answer_service.answer_meta(ctx_b, Recall())   # 权限不同 → 未命中
        return a1, a2, b1

    a1, a2, b1 = asyncio.run(main())
    assert a1 == a2 == "回答第 1 次"
    assert b1 == "回答第 2 次"
    assert calls["n"] == 2


def test_meta_answer_key_follows_reference_text(monkeypatch):
    """知识库内容一改（资料指纹变），旧缓存立即失效，不需要额外通知。"""
    from app.services import intent as intent_mod
    from app.core.statemachine import PipelineContext

    monkeypatch.setattr(settings, "llm_mock", False)
    monkeypatch.setattr(intent_mod, "_visible_tables", lambda _u: "inherit")
    calls = {"n": 0}

    async def fake_chat(prompt, system=""):
        calls["n"] += 1
        return f"版本 {calls['n']}"

    monkeypatch.setattr(intent_mod.llm_client, "chat", fake_chat)
    question = f"缓存验证-{uuid.uuid4().hex} 完播率怎么算"

    class RecallOld:
        knowledge = [{"term": "完播率", "definition": "旧定义", "formula": "a/b"}]
        calibers = []
        tables = []

    class RecallNew:
        knowledge = [{"term": "完播率", "definition": "新定义", "formula": "a/c"}]
        calibers = []
        tables = []

    async def main():
        ctx = PipelineContext(question=question, user_id="u_c", user_role="user")
        first = await intent_mod.answer_service.answer_meta(ctx, RecallOld())
        again = await intent_mod.answer_service.answer_meta(ctx, RecallOld())
        changed = await intent_mod.answer_service.answer_meta(ctx, RecallNew())
        return first, again, changed

    first, again, changed = asyncio.run(main())
    assert first == again == "版本 1"
    assert changed == "版本 2"
    assert calls["n"] == 2
