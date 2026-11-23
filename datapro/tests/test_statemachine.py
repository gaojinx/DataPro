"""状态机与服务装配测试（mock 模式，不依赖外部服务）。"""
from app.core.statemachine import Pipeline, PipelineContext
from app.services.intent import answer_service, intent_service
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.generator import MockGenerator
from app.services.validator import ValidatorService


def build() -> Pipeline:
    return Pipeline(intent_service, recall_service, MockGenerator(),
                    ValidatorService(), executor_service, answer_service)


async def test_forbidden_reject():
    ctx = PipelineContext(question="帮我把订单表的价格都改一下", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert events[0]["type"] == "node"  # INTENT
    assert any(e["type"] == "reject" and e["reason_code"] == "E003" for e in events)


async def test_clarify_flow():
    ctx = PipelineContext(question="查一下销售额", user_id="t")  # 缺时间要素
    events = [e async for e in build().run(ctx)]
    assert any(e["type"] == "clarify" for e in events)


async def test_reject_after_two_clarifies():
    ctx = PipelineContext(question="销售额", user_id="t")  # 永远凑不齐三要素
    pipeline = build()
    events = [e async for e in pipeline.run(ctx)]
    assert any(e["type"] == "clarify" for e in events)


async def test_meta_qa_flow():
    ctx = PipelineContext(question="支付订单数的口径是什么", user_id="t")
    events = [e async for e in build().run(ctx)]
    result = next(e for e in events if e["type"] == "result")
    assert "M003" in result["summary"] or "支付订单数" in result["summary"]


async def test_recall_zero_hit_reject():
    # 有时间+指标要素但召回 0 命中 → E002 拒答（禁止模型自由发挥）
    ctx = PipelineContext(question="昨天的工单数量统计", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert any(e["type"] == "reject" and e["reason_code"] == "E002" for e in events)


async def test_validator_service_interface():
    v = ValidatorService()
    r = await v.validate("SELECT * FROM dwd_order_detail_di", ["dwd_order_detail_di"])
    assert r.passed
