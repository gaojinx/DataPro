"""无 LLM 时的口径模板降级：多日窗口必须按区间查，不能只查第一天。"""
from datetime import date

from app.core.statemachine import PipelineContext
from app.core.time_window import TimeWindow
from app.services.generator import GeneratorService


def _ctx(window: TimeWindow | None) -> PipelineContext:
    ctx = PipelineContext(question="最近7天支付订单数", user_id="u", user_role="user")
    ctx.recalled_calibers = [{
        "metric_id": "M003", "metric_name": "支付订单数",
        "logic": "COUNT(DISTINCT order_id) WHERE pay_status=1 AND dt='{date}'",
        "table": "dwd_order_detail_di",
    }]
    ctx.recalled_tables = ["dwd_order_detail_di"]
    ctx.time_window = window
    return ctx


def test_degraded_sql_covers_whole_window():
    w = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "最近 7 天")
    gen = GeneratorService._degraded(_ctx(w))
    assert gen is not None
    assert "dt BETWEEN '{date_start}' AND '{date_end}'" in gen.sql
    assert "{date}" not in gen.sql


def test_degraded_assumption_states_real_window():
    w = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "最近 7 天")
    gen = GeneratorService._degraded(_ctx(w))
    assert any("2026-11-16" in a and "2026-11-22" in a for a in gen.assumptions)


def test_degraded_without_caliber_returns_none():
    ctx = PipelineContext(question="多少钱", user_id="u", user_role="user")
    assert GeneratorService._degraded(ctx) is None


def _ctx_with(caliber: dict, window=None) -> PipelineContext:
    ctx = PipelineContext(question="最近7天指标", user_id="u", user_role="user")
    ctx.recalled_calibers = [caliber]
    ctx.recalled_tables = [caliber.get("table") or "dwd_order_detail_di"]
    ctx.time_window = window
    return ctx


def test_sum_caliber_is_templateable():
    """SUM 类指标（销售额/销量/花费）此前只认 COUNT，会直接转人工。"""
    gen = GeneratorService._degraded(_ctx_with({
        "metric_id": "M006", "metric_name": "销售额", "table": "dwd_order_detail_di",
        "logic": "SUM(amount) WHERE pay_status=1 AND dt='{date}'"}))
    assert gen is not None
    assert gen.sql.startswith("SELECT SUM(amount) AS m006 FROM dwd_order_detail_di")
    assert "pay_status=1" in gen.sql


def test_round_caliber_with_boolean_sum_and_nullif():
    """退货率用 SUM(pay_status=2)/SUM(pay_status=1)，ROI 用 NULLIF 防除零。"""
    gen = GeneratorService._degraded(_ctx_with({
        "metric_id": "M012", "metric_name": "退货率", "table": "dwd_order_detail_di",
        "logic": "ROUND(SUM(pay_status=2)/SUM(pay_status=1)*100,2) WHERE dt='{date}'"}))
    assert gen is not None and "ROUND(" in gen.sql and "SUM(pay_status=2)" in gen.sql

    roi = GeneratorService._degraded(_ctx_with({
        "metric_id": "M014", "metric_name": "广告ROI", "table": "dwd_campaign_daily",
        "logic": "ROUND(SUM(gmv)/NULLIF(SUM(cost),0),2) WHERE dt='{date}'"}))
    assert roi is not None and "NULLIF(SUM(cost),0)" in roi.sql


def test_date_placeholder_on_non_dt_column_is_rewritten():
    """新增用户（注册口径）的日期列是 register_date：只重写 dt 会留下字面 '{date}'。"""
    w = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "最近 7 天")
    gen = GeneratorService._degraded(_ctx_with({
        "metric_id": "M004", "metric_name": "新增用户数（注册口径）", "table": "dwd_user_di",
        "logic": "COUNT(DISTINCT user_id) WHERE register_date='{date}'"}, w))
    assert gen is not None
    assert "register_date BETWEEN '{date_start}' AND '{date_end}'" in gen.sql
    assert "{date}" not in gen.sql


def test_unknown_placeholder_falls_back_to_manual():
    """口径里出现模板不认识的占位符 → 不发 SQL，转人工。"""
    assert GeneratorService._degraded(_ctx_with({
        "metric_id": "M099", "metric_name": "月累计", "table": "dwd_order_detail_di",
        "logic": "SUM(amount) WHERE dt BETWEEN '{month_start}' AND '{month_end}'"})) is None


def test_unsafe_expression_is_rejected():
    """表达式白名单：带引号/分号的写法不模板化（口径库被误填时不能拼任意 SQL）。"""
    for logic in ("SUM(amount); DROP TABLE x WHERE dt='{date}'",
                  "SUM(amount) FROM x WHERE 1=1 AND dt='{date}'"):
        assert GeneratorService._degraded(_ctx_with({
            "metric_id": "M098", "metric_name": "坏口径",
            "table": "dwd_order_detail_di", "logic": logic})) is None


def test_non_aggregate_logic_is_rejected():
    """没有聚合函数的 logic 不是指标，不能当模板发出去。"""
    assert GeneratorService._degraded(_ctx_with({
        "metric_id": "M097", "metric_name": "裸列", "table": "dwd_order_detail_di",
        "logic": "amount WHERE dt='{date}'"})) is None


def test_falls_through_to_next_caliber_when_first_is_unusable():
    ctx = PipelineContext(question="q", user_id="u", user_role="user")
    ctx.recalled_calibers = [
        {"metric_id": "M096", "metric_name": "无 WHERE", "table": "dwd_order_detail_di",
         "logic": "SUM(amount)"},
        {"metric_id": "M006", "metric_name": "销售额", "table": "dwd_order_detail_di",
         "logic": "SUM(amount) WHERE pay_status=1 AND dt='{date}'"},
    ]
    ctx.recalled_tables = ["dwd_order_detail_di"]
    gen = GeneratorService._degraded(ctx)
    assert gen is not None and "m006" in gen.sql


def test_all_seeded_calibers_are_templateable():
    """种子口径必须全部能模板化：任何一条写法超出模板能力，就等于该指标永远转人工。"""
    from app.services.recall import SEED_CALIBERS

    for c in SEED_CALIBERS:
        gen = GeneratorService._degraded(_ctx_with(c))
        assert gen is not None, f"{c['metric_id']} {c['metric_name']} 无法模板化：{c['logic']}"
        assert "{date}" not in gen.sql
