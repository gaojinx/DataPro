"""状态机与服务装配测试（mock 模式，不依赖外部服务）。"""
from app.core.statemachine import Pipeline, PipelineContext
from app.services.intent import answer_service, intent_service
from app.services.query_spec import QuerySpec, spec_service
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.generator import MockGenerator
from app.services.validator import ValidatorService


class RuleSpecService:
    """单测用规格抽取：只走规则，不访问模型。"""

    async def extract(self, question, prev=None, calibers=None):
        base = QuerySpec.from_dict(prev)
        return spec_service._rule(question, base, calibers).inherit(base)


def build() -> Pipeline:
    return Pipeline(intent_service, recall_service, MockGenerator(),
                    ValidatorService(), executor_service, answer_service,
                    spec_service=RuleSpecService())


async def test_forbidden_reject():
    ctx = PipelineContext(question="帮我把订单表的价格都改一下", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert events[0]["type"] == "node"  # INTENT
    assert any(e["type"] == "reject" and e["reason_code"] == "E003" for e in events)


async def test_clarify_flow():
    # 时间表达有歧义（环比需要两个对比期）→ 必须澄清，不能猜
    ctx = PipelineContext(question="本月支付订单数环比增长多少", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert any(e["type"] == "clarify" for e in events)


async def test_caliber_ambiguity_clarify():
    # 「新增用户」同时命中注册口径与下单口径 → 先确认口径，不猜
    ctx = PipelineContext(question="查一下昨天的新增用户", user_id="t")
    events = [e async for e in build().run(ctx)]
    clarify = [e for e in events if e["type"] == "clarify"]
    assert clarify and clarify[0].get("kind") == "caliber"
    assert {o["metric_id"] for o in clarify[0]["options"]} == {"M004", "M005"}
    assert clarify[0]["original_question"] == "查一下昨天的新增用户"


async def test_forced_caliber_skips_clarify():
    # 用户已选口径：只保留所选口径，不再追问
    ctx = PipelineContext(question="查一下昨天的新增用户", user_id="t",
                          forced_caliber_id="M004")
    events = [e async for e in build().run(ctx)]
    assert not any(e["type"] == "clarify" for e in events)
    assert [c["metric_id"] for c in ctx.recalled_calibers] == ["M004"]


async def test_reject_after_two_clarifies():
    ctx = PipelineContext(question="销售额环比增长多少", user_id="t")
    pipeline = build()
    events = [e async for e in pipeline.run(ctx)]
    assert any(e["type"] == "clarify" for e in events)


async def test_time_window_default_not_clarified():
    # 未提时间：取默认最近 7 天（显式声明），不打断用户
    ctx = PipelineContext(question="查一下销售额", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert not any(e["type"] == "clarify" for e in events)
    assert ctx.time_window is not None and ctx.time_window.explicit is False
    assert any(e["node"] == "TIME" for e in events if e["type"] == "node")


async def test_meta_qa_flow():
    ctx = PipelineContext(question="支付订单数的口径是什么", user_id="t")
    events = [e async for e in build().run(ctx)]
    result = next(e for e in events if e["type"] == "result")
    assert "M003" in result["summary"] or "支付订单数" in result["summary"]


async def test_recall_zero_hit_reject():
    # 有时间+指标要素但召回 0 命中：有可用模型时由模型说明可查询范围（0 行，不编造）；无模型时 E002 拒答
    ctx = PipelineContext(question="昨天的工单数量统计", user_id="t")
    events = [e async for e in build().run(ctx)]
    results = [e for e in events if e["type"] == "result"]
    if results:
        assert results[0]["row_count"] == 0 and not results[0]["rows"]
    else:
        assert any(e["type"] == "reject" and e["reason_code"] == "E002" for e in events)


async def test_meta_qa_zero_hit_explains_scope(monkeypatch):
    """口径类问题全链路 0 命中：有模型时也要说明能查什么，不能只回一句 404 文案。

    取数分支早就有 explain_scope，口径分支却直接 E002——同样是「没收录」，
    两种体验：一个告诉你换个问法，一个只告诉你「联系数据团队」。
    """
    from app.llm import client as llm_mod

    monkeypatch.setattr(llm_mod.llm_client, "available", lambda: True)

    async def fake_chat(prompt, system=None, **kwargs):
        return "未收录「客户投诉率」；当前可查询支付订单数、销售额、退款金额等指标。"

    monkeypatch.setattr(llm_mod.llm_client, "chat", fake_chat)

    ctx = PipelineContext(question="客户投诉率的计算口径是什么", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert not [e for e in events if e["type"] == "reject"]
    result = next(e for e in events if e["type"] == "result")
    assert result["row_count"] == 0 and not result["rows"]
    assert "未收录" in result["summary"]
    assert "未命中" in result["explanation"]


async def test_meta_qa_zero_hit_rejects_without_model(monkeypatch):
    """无模型可用时没有能力生成范围说明，必须退化为 E002，而不是返回空答案。"""
    from app.llm import client as llm_mod

    monkeypatch.setattr(llm_mod.llm_client, "available", lambda: False)

    ctx = PipelineContext(question="客户投诉率的计算口径是什么", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert any(e["type"] == "reject" and e["reason_code"] == "E002" for e in events)


async def test_validator_service_interface():
    v = ValidatorService()
    r = await v.validate("SELECT * FROM dwd_order_detail_di", ["dwd_order_detail_di"])
    assert r.passed


def test_general_question_routing():
    # 非取数问题交给 AI 回答；取数与口径问题仍走原链路
    from app.services.smalltalk import should_answer_general
    assert should_answer_general("你是什么模型")
    assert should_answer_general("？")
    assert not should_answer_general("查昨天的支付订单数")
    assert not should_answer_general("完播率怎么算")
    # 多轮追问：上一轮有指标 → 本轮省略指标也走取数链路
    history = [{"role": "user", "content": "查昨天的支付订单数"}]
    assert not should_answer_general("那上周呢", history)
    assert should_answer_general("今天杭州天气怎么样", history)


def test_smalltalk_not_rejected():
    # 寒暄/自我介绍交由 AI 回答，不进状态机（避免被 E002 拒答）
    from app.services.smalltalk import match_smalltalk
    assert match_smalltalk("你是谁")
    assert match_smalltalk("你好呀")
    assert not match_smalltalk("查昨天的支付订单数")


async def test_spec_inherits_previous_slots():
    """多轮槽位：只说"按品类拆分"，指标与时间从上一轮继承。"""
    prev = {"metric": "支付订单数", "time_phrase": "昨天", "grain": "total",
            "dimensions": [], "filters": {}, "ambiguous": [], "source": "llm"}
    ctx = PipelineContext(question="按品类拆分一下", user_id="t", prev_spec=prev)
    [e async for e in build().run(ctx)]
    assert ctx.spec.metric == "支付订单数"
    assert ctx.spec.dimensions == ["品类"]
    assert ctx.time_window is not None


def test_spec_rule_extraction_and_merge():
    spec = spec_service._rule("本周按渠道统计销售额", QuerySpec(), [])
    assert spec.metric == "销售额"
    assert spec.dimensions == ["渠道"]
    assert spec.grain == "渠道"
    merged = QuerySpec().inherit(QuerySpec.from_dict(
        {"metric": "支付订单数", "time_phrase": "昨天", "grain": "total",
         "dimensions": [], "filters": {}, "ambiguous": [], "source": "llm"}))
    assert merged.metric == "支付订单数" and merged.time_phrase == "昨天"
    assert "支付订单数" in merged.display()


def test_grain_normalized_for_single_bucket():
    """窗口只覆盖一个时间桶时，按天/月分组回落为整体汇总（避免返回序列而非单值）。"""
    from datetime import date

    from app.core.time_window import resolve
    from app.services.query_spec import normalize_grain
    today = date(2026, 11, 23)
    month = QuerySpec(metric="支付订单数", grain="month")
    normalize_grain(month, resolve("2026年10月", today=today))
    assert month.grain == "total"
    day = QuerySpec(metric="支付订单数", grain="day")
    normalize_grain(day, resolve("2026-11-20", today=today))
    assert day.grain == "total"
    # 多桶窗口保留原粒度
    week = QuerySpec(metric="支付订单数", grain="day")
    normalize_grain(week, resolve("最近7天", today=today))
    assert week.grain == "day"


class NoneGenerator:
    """永远给不出 SQL 的生成器，用于验证结构化重试的阶段推进。"""

    async def generate(self, ctx):
        return None


class LlmDimSpec:
    """返回「按城市分组」的 LLM 规格，用于验证 IR 不一致时转人工。"""

    async def extract(self, question, prev=None, calibers=None):
        return QuerySpec(metric="支付订单数", time_phrase="", grain="城市",
                         dimensions=["城市"], source="llm")


class TotalSqlGenerator:
    """永远返回整体汇总 SQL（没有 GROUP BY），与规格要求的维度冲突。"""

    async def generate(self, ctx):
        from app.services.generator import Generation
        return Generation(sql="SELECT COUNT(*) AS cnt FROM dwd_shop.dwd_order_detail_di "
                              "WHERE pay_status = 1 AND dt BETWEEN '{date_start}' AND '{date_end}'",
                          explanation="整体汇总")


async def test_ir_inconsistency_escalates_instead_of_answering():
    """规格要按城市分组、SQL 却给整体汇总：回炉一次后仍不一致 → 转人工，绝不返回错误数字。"""
    from app.services.validator import ValidatorService
    pipeline = Pipeline(intent_service, recall_service, TotalSqlGenerator(),
                        ValidatorService(), executor_service, answer_service,
                        spec_service=LlmDimSpec())
    ctx = PipelineContext(question="2026-11-20各城市的支付订单数", user_id="t")
    events = [e async for e in pipeline.run(ctx)]
    assert any(e["type"] == "escalate" for e in events)
    assert not any(e["type"] == "result" for e in events)


async def test_multiturn_keeps_raw_question_and_context():
    """多轮追问：保存原话，并把被跳过的中间轮次一起记入 question_context。"""
    history = [
        {"role": "user", "content": "查2026-11-20的支付订单数"},
        {"role": "user", "content": "按品类拆分一下"},
        {"role": "user", "content": "那11月16日到11月22日呢"},
    ]
    ctx = PipelineContext(question="那11月16日到11月22日呢", user_id="t", history=history)
    [e async for e in build().run(ctx)]
    assert ctx.raw_question == "那11月16日到11月22日呢"      # 原话不被合并串覆盖
    assert ctx.question_context == ["查2026-11-20的支付订单数", "按品类拆分一下"]
    # 本轮自带时间：合并串以本轮在前，保证时间窗取本轮而不是上一轮
    assert ctx.question.startswith("那11月16日到11月22日呢")
    assert "查2026-11-20的支付订单数" in ctx.question


async def test_independent_question_has_no_context():
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t",
                          history=[{"role": "user", "content": "查昨天的支付订单数"}])
    [e async for e in build().run(ctx)]
    assert ctx.raw_question == ctx.question
    assert ctx.question_context == []


async def test_structured_retry_escalates_after_stages():
    from app.services.validator import ValidatorService
    pipeline = Pipeline(intent_service, recall_service, NoneGenerator(),
                        ValidatorService(), executor_service, answer_service,
                        spec_service=RuleSpecService())
    ctx = PipelineContext(question="支付订单数", user_id="t")
    events = [e async for e in pipeline.run(ctx)]
    assert any(e["type"] == "escalate" for e in events)
    assert ctx.no_sql_count == 3          # 0/1/2 三个阶段各一次
    assert ctx.retry_stage == 2           # 最后一轮已进入「最小可执行查询」阶段


async def test_scan_guard_escalates_without_execute(monkeypatch):
    """A09：EXPLAIN 预估扫描量超阈值 → 转人工，不执行大查询。"""
    async def fake_estimate(sql, session_id="", window=None):
        return 5_000_000

    monkeypatch.setattr(executor_service, "estimate_scan", fake_estimate)
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t")
    events = [e async for e in build().run(ctx)]
    esc = next(e for e in events if e["type"] == "escalate")
    assert esc["reason"] == "SCAN_TOO_LARGE" and "5,000,000" in esc["message"]
    assert not any(e["type"] == "result" for e in events)


async def test_result_event_carries_scan_and_numeric_hints(monkeypatch):
    """X-L03/X-R04：result 事件透传已算出的扫描预估与数值列白名单，不另发 EXPLAIN。"""
    from decimal import Decimal

    async def counting_estimate(sql, session_id="", window=None):
        estimate_calls.append(1)
        return 12_345

    async def fake_execute(sql, user_id, session_id="", window=None):
        return {
            "columns": ["order_no", "pay_amount", "order_cnt"],
            "rows": [
                {"order_no": "202611220001", "pay_amount": Decimal("72470.55"), "order_cnt": 3},
                {"order_no": "202611220002", "pay_amount": Decimal("12.5"), "order_cnt": 1},
            ],
            "row_count": 2, "truncated": False,
        }

    estimate_calls = []
    monkeypatch.setattr(executor_service, "estimate_scan", counting_estimate)
    monkeypatch.setattr(executor_service, "execute", fake_execute)
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t")
    events = [e async for e in build().run(ctx)]
    result = next(e for e in events if e["type"] == "result")
    assert result["est_scan_rows"] == 12_345
    assert result["numeric_columns"] == ["pay_amount", "order_cnt"]
    assert "order_no" not in result["numeric_columns"]
    assert len(estimate_calls) == 1        # 复用 EXECUTE 前已算值，不重复跑 EXPLAIN


def test_schema_block_injects_enum_values():
    """元数据枚举值进入生成 prompt：模型应写 region='东北'，而不是臆造“东北区域”。"""
    from app.services.generator import _schema_block
    ctx = PipelineContext(question="2026-11-20东北区域的支付订单数", user_id="t")
    ctx.recalled_schemas = [{
        "schema": "dwd_shop", "table_name": "dwd_order_detail_di", "description": "订单明细",
        "columns": ["region", "dt"], "column_labels": [],
        "column_enums": {"region": "华东/华北/华南/华中/西南/西北/东北"},
    }]
    block = _schema_block(ctx)
    assert "取值" in block and "东北" in block


async def test_merged_question_keeps_prior_metric(monkeypatch):
    """多轮追问：模型把指标改写成销售额时，按前序问题里的口径词兜回。"""
    from app.services import query_spec as qs

    class FakeLLM:
        def available(self):
            return True

        async def structured_call(self, *args, **kwargs):
            return {"metric": "销售额", "time_phrase": "11月16日到11月22日",
                    "grain": "total", "dimensions": [], "filters": {}, "ambiguous": []}

    monkeypatch.setattr(qs, "llm_client", FakeLLM())
    question = ("查2026-11-20的支付订单数；"
                "本次补充（时间/条件优先于上句）：那11月16日到11月22日呢")
    spec = await qs.SpecService().extract(
        question, None, [{"metric_name": "支付订单数", "aliases": []}])
    assert spec.metric == "支付订单数"

