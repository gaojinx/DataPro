"""多轮指代增强单测：槽位替换（时间/指标/维度）与省略追问。"""
from datetime import date

from app.core.statemachine import PipelineContext
from app.core.time_window import resolve
from tests.test_statemachine import build


PREV = {"metric": "支付订单数", "time_phrase": "昨天", "grain": "total",
        "dimensions": [], "filters": {}, "ambiguous": [], "source": "llm"}


def _ctx(question: str, history: list[str], prev: dict | None = None) -> PipelineContext:
    # 与线上一致：history 末尾是当前问题；prev_spec 来自会话上一轮 IR
    msgs = [{"role": "user", "content": h} for h in history]
    msgs.append({"role": "user", "content": question})
    return PipelineContext(question=question, user_id="t", history=msgs,
                           prev_spec=dict(prev) if prev else None)


async def test_replace_time_uses_new_window():
    # “换成上周”：合并句里本轮在前，时间解析必须取上周而不是上一轮的昨天
    ctx = _ctx("换成上周", ["查昨天的支付订单数"])
    [e async for e in build().run(ctx)]
    expected = resolve("上周", today=date.today())
    assert ctx.time_window is not None
    assert (ctx.time_window.start, ctx.time_window.end) == (expected.start, expected.end)
    assert "上周" in ctx.question


async def test_pronoun_inherits_metric():
    # “它上周呢”：只有指代 + 时间，指标从上一轮继承
    ctx = _ctx("它上周呢", ["查昨天的支付订单数"], PREV)
    [e async for e in build().run(ctx)]
    assert ctx.spec is not None and ctx.spec.metric
    expected = resolve("上周", today=date.today())
    assert ctx.time_window and ctx.time_window.start == expected.start


async def test_add_dimension_turn():
    # “再按渠道拆一层”：继承指标与时间，补上渠道维度
    ctx = _ctx("再按渠道拆一层", ["查昨天的支付订单数"], PREV)
    [e async for e in build().run(ctx)]
    assert "渠道" in (ctx.spec.dimensions if ctx.spec else [])


async def test_replace_dimension_not_append():
    # “维度换成渠道”：覆盖上一轮的品类维度，而不是叠加
    ctx = _ctx("维度换成渠道", ["查昨天按品类分组的支付订单数"])
    [e async for e in build().run(ctx)]
    assert ctx.spec is not None
    assert ctx.spec.dimensions == ["渠道"]


async def test_replace_metric():
    # “换成销售额”：指标覆盖，时间沿用上一轮
    ctx = _ctx("换成销售额", ["查2026-11-20的支付订单数"])
    [e async for e in build().run(ctx)]
    assert ctx.spec is not None and ctx.spec.metric == "销售额"
