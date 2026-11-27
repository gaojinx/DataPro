"""LLM 接入层单测（B09/B17）与执行引擎日期注入（B06）。"""
from datetime import date

from app.core.time_window import default_window, resolve
from app.llm.client import _parse_json
from app.services.executor import inject_dates

TODAY = date(2026, 11, 21)


def test_parse_json_plain():
    assert _parse_json('{"sql": "SELECT 1"}')["sql"] == "SELECT 1"


def test_parse_json_markdown_fence():
    raw = '```json\n{"sql": "SELECT 2", "confidence": "high"}\n```'
    assert _parse_json(raw)["sql"] == "SELECT 2"


def test_parse_json_noise_around_object():
    raw = '好的，结果如下：{"sql": "SELECT 3"} 以上。'
    assert _parse_json(raw)["sql"] == "SELECT 3"


def test_parse_json_invalid_returns_none():
    assert _parse_json("这不是 JSON") is None
    assert _parse_json("") is None


def test_inject_dates_default_last_7_days():
    sql = "SELECT * FROM t WHERE dt >= '{start_date}' AND dt <= '{end_date}'"
    out = inject_dates(sql, default_window(TODAY))
    assert "2026-11-20" in out          # end_date = T-1
    assert "2026-11-14" in out          # start_date = T-7
    assert "{start_date}" not in out


def test_inject_dates_single_date():
    w = resolve("昨天", today=TODAY)
    assert inject_dates("WHERE dt='{date}'", w) == "WHERE dt='2026-11-20'"


def test_inject_dates_leaves_other_braces():
    sql = "SELECT '{not_a_date}'"
    assert inject_dates(sql, default_window(TODAY)) == sql


async def test_generator_uses_sql_fix_on_retry(monkeypatch):
    """B10：有上一版 SQL 与校验错误时走 sql_fix 定向修正，而不是从零重写。"""
    from app.core.statemachine import PipelineContext
    from app.services import generator as gen_mod

    captured = {}

    class FakeClient:
        def available(self):
            return True

        async def structured_call(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["version"] = kwargs.get("tool_version")
            return {"sql": "SELECT 1 AS ok", "explanation": "修正后"}

    monkeypatch.setattr(gen_mod, "llm_client", FakeClient())
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t",
                          generated_sql="SELECT bad FROM dwd_order_detail_di",
                          validation_errors=["字段不存在: bad"], retry_stage=1)
    gen = await gen_mod.GeneratorService().generate(ctx)

    assert gen is not None and gen.sql == "SELECT 1 AS ok"
    assert "待修正 SQL" in captured["prompt"]
    assert "SELECT bad FROM dwd_order_detail_di" in captured["prompt"]
    assert "字段不存在: bad" in captured["prompt"]
    assert captured["version"] == "v2"
