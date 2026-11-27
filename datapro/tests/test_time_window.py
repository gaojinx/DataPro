"""时间语义解析与字段校验测试（确定性，不依赖 LLM/数据库）。"""
from datetime import date

from app.core.time_window import default_window, is_ambiguous_time, resolve, resolve_question
from app.services.executor import inject_dates
from app.services.validator import validate_sql

TODAY = date(2026, 11, 23)   # 周一，数仓最新分区为 2026-11-22


def _w(text: str):
    return resolve(text, today=TODAY)


def test_relative_day():
    assert (_w("昨天").start, _w("昨天").end) == (date(2026, 11, 22), date(2026, 11, 22))
    assert (_w("前天").start, _w("前天").end) == (date(2026, 11, 21), date(2026, 11, 21))


def test_last_month_is_not_last_7_days():
    """核心回归：问上月必须查上月，不能静默变成最近 7 天。"""
    w = _w("上个月的支付订单数")
    assert (w.start, w.end) == (date(2026, 10, 1), date(2026, 10, 31))
    assert w.label == "上月"


def test_week_and_month_windows():
    # 周一当天：本周只有今天（数据尚未产出，由 0 行自检说明）
    assert (_w("本周").start, _w("本周").end) == (date(2026, 11, 23), date(2026, 11, 23))
    assert (_w("上周").start, _w("上周").end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert (_w("本月").start, _w("本月").end) == (date(2026, 11, 1), date(2026, 11, 22))


def test_recent_n():
    assert (_w("最近7天").start, _w("最近7天").end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert (_w("近30天").start, _w("近30天").end) == (date(2026, 10, 24), date(2026, 11, 22))
    assert _w("最近一个月").grain == "month"


def test_explicit_range_and_month():
    w = _w("2026-11-16到2026-11-22的支付订单数")
    assert (w.start, w.end) == (date(2026, 11, 16), date(2026, 11, 22))
    w = _w("2026年10月的支付订单数是多少")
    assert (w.start, w.end) == (date(2026, 10, 1), date(2026, 10, 31))


def test_ambiguous_time_needs_clarify():
    assert resolve_question("本月支付订单数环比增长多少", TODAY) == (None, True)
    assert resolve_question("3月比2月订单量增长多少", TODAY) == (None, True)
    assert is_ambiguous_time("最近的支付订单数") is False
    # 明确区间不算歧义（"11月"出现两次不应误判为两期对比）
    assert is_ambiguous_time("11月16日到11月22日") is False
    assert resolve_question("11月16日到11月22日", TODAY)[0] is not None


def test_no_time_is_not_ambiguous():
    window, ambiguous = resolve_question("查一下销售额", TODAY)
    assert window is None and ambiguous is False
    d = default_window(TODAY)
    assert d.explicit is False and (d.start, d.end) == (date(2026, 11, 16), date(2026, 11, 22))


def test_inject_dates_binds_resolved_window():
    w = _w("上个月")
    sql = "SELECT 1 FROM t WHERE dt BETWEEN '{date_start}' AND '{date_end}'"
    assert inject_dates(sql, w) == \
        "SELECT 1 FROM t WHERE dt BETWEEN '2026-10-01' AND '2026-10-31'"
    # 历史别名兼容：{date} 绑定到窗口起点，不再固定为 T-1
    assert "'2026-10-01'" in inject_dates("SELECT 1 WHERE dt='{date}'", w)


def test_validator_column_existence():
    cmap = {"dwd_order_detail_di": {"order_id", "pay_status", "amount", "dt", "category"}}
    ok = validate_sql(
        "SELECT category, COUNT(*) AS cnt FROM dwd_order_detail_di "
        "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY category ORDER BY cnt DESC",
        {"dwd_order_detail_di"}, column_map=cmap)
    assert ok.passed, ok.errors
    bad = validate_sql("SELECT order_cnt FROM dwd_order_detail_di WHERE dt='{date_start}'",
                       {"dwd_order_detail_di"}, column_map=cmap)
    assert not bad.passed and any("不存在的字段" in e for e in bad.errors)
