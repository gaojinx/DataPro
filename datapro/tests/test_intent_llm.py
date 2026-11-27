"""LLM 意图识别（第 3 项）单测：覆盖、兜底与安全优先，不调用真实模型。"""
from app.config import settings
from app.core.statemachine import PipelineContext
from app.services import intent_llm
from app.services.intent import intent_service


def _ctx(question: str) -> PipelineContext:
    return PipelineContext(question=question, user_id="t")


async def test_llm_meta_overrides_query_rule(monkeypatch):
    monkeypatch.setattr(settings, "llm_mock", False)

    async def fake(question, history=None):
        return "meta_qa"

    monkeypatch.setattr(intent_llm, "classify", fake)
    assert await intent_service.classify(_ctx("销售额是多少")) == "meta_qa"


async def test_llm_reject_maps_to_forbidden(monkeypatch):
    monkeypatch.setattr(settings, "llm_mock", False)

    async def fake(question, history=None):
        return "reject"

    monkeypatch.setattr(intent_llm, "classify", fake)
    assert await intent_service.classify(_ctx("查昨天的支付订单数")) == "forbidden"


async def test_forbidden_rule_not_overridden(monkeypatch):
    monkeypatch.setattr(settings, "llm_mock", False)

    async def fake(question, history=None):
        return "query"

    monkeypatch.setattr(intent_llm, "classify", fake)
    assert await intent_service.classify(_ctx("帮我把订单表的价格都改一下")) == "forbidden"


async def test_smalltalk_ignored_in_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "llm_mock", False)

    async def fake(question, history=None):
        return "smalltalk"

    monkeypatch.setattr(intent_llm, "classify", fake)
    assert await intent_service.classify(_ctx("查昨天的支付订单数")) == "query"


async def test_mock_mode_skips_llm(monkeypatch):
    # mock 模式直接返回 None（不发起模型调用），流水线保持规则判定
    monkeypatch.setattr(settings, "llm_mock", True)
    assert await intent_llm.classify("查昨天的支付订单数") is None
    assert await intent_service.classify(_ctx("查昨天的支付订单数")) == "query"


async def test_llm_failure_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(settings, "llm_mock", False)

    async def fake(question, history=None):
        return None

    monkeypatch.setattr(intent_llm, "classify", fake)
    assert await intent_service.classify(_ctx("支付订单数的口径是什么")) == "meta_qa"
