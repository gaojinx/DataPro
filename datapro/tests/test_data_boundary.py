"""数据边界问答（「数据最新到几号」「数据覆盖哪些日期」）单测。"""
from app.core.statemachine import Pipeline, PipelineContext
from app.services import data_boundary
from app.services.intent import answer_service, intent_service
from app.services.query_spec import QuerySpec, spec_service
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.generator import MockGenerator
from app.services.validator import ValidatorService


class RuleSpecService:
    async def extract(self, question, prev=None, calibers=None):
        base = QuerySpec.from_dict(prev)
        return spec_service._rule(question, base, calibers).inherit(base)


def build() -> Pipeline:
    return Pipeline(intent_service, recall_service, MockGenerator(),
                    ValidatorService(), executor_service, answer_service,
                    spec_service=RuleSpecService())


def test_detect_boundary_questions():
    for q in ("数据库最新的数据到几号", "数据更新到几号了", "数据截止到哪天",
              "数仓最新分区是哪天", "数据覆盖哪些日期", "数据日期范围是什么",
              "有多少天的数据", "订单表里最新的数据是哪天的"):
        assert data_boundary.detect(q), q
    # 普通取数问题不能被拦成数据边界
    for q in ("最近7天的销售额", "昨天的支付订单数", "看最近的销售数据",
              "给我最新的数据", "按天看11月销售额"):
        assert not data_boundary.detect(q), q


def test_build_summary_latest_and_coverage():
    rows = [{"表": "dwd_order_detail_di", "最早分区": "2026-11-13",
             "最新分区": "2026-11-22", "天数": 10},
            {"表": "dwd_refund_detail_di", "最早分区": "2026-11-13",
             "最新分区": "2026-11-20", "天数": 8}]
    text = data_boundary.build_summary(rows)
    assert "2026-11-22" in text and "2026-11-13" in text
    assert "dwd_refund_detail_di" in text


async def test_boundary_question_answered_directly(monkeypatch):
    """「数据最新到几号」直接读分区作答，不走澄清/拒答。"""
    async def fake_probe(user_id="", session_id="", user_role="user", question=""):
        return {"columns": ["表", "最早分区", "最新分区", "天数"],
                "rows": [{"表": "dwd_order_detail_di", "最早分区": "2026-11-13",
                          "最新分区": "2026-11-22", "天数": 10}],
                "row_count": 1}

    monkeypatch.setattr(data_boundary, "probe", fake_probe)
    ctx = PipelineContext(question="数据库最新的数据到几号", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert not any(e["type"] in ("clarify", "reject") for e in events)
    result = next(e for e in events if e["type"] == "result")
    assert "2026-11-22" in result["summary"]
    assert result["row_count"] == 1
