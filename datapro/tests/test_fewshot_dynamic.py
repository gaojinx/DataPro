"""动态 few-shot（A06）单测：占位符替换、硬编码日期跳过、动态示例参与检索。"""
import json

from app.services import fewshot


def _case(sql, start="2026-11-16", end="2026-11-22", question="2026-11-16到2026-11-22的销售额合计"):
    return {"case_id": "G900", "question": question, "expected_sql": sql,
            "expected_tables": ["dwd_order_detail_di"],
            "expect": {"time_window": {"start": start, "end": end}}}


def test_to_example_replaces_fixed_dates():
    from eval.promote_fewshot import to_example

    ex = to_example(_case("SELECT SUM(amount) FROM dwd_order_detail_di "
                          "WHERE dt BETWEEN '2026-11-16' AND '2026-11-22'"))
    assert ex and ex["grain"] == "total" and ex["metric"] == "sum"
    assert "{date_start}" in ex["sql"] and "{date_end}" in ex["sql"]


def test_to_example_skips_unreplaceable_dates():
    from eval.promote_fewshot import to_example

    assert to_example(_case("SELECT 1 FROM dwd_order_detail_di WHERE dt = '2020-01-01'")) is None


def test_to_example_detects_dimension_grain():
    from eval.promote_fewshot import to_example

    ex = to_example(_case("SELECT category, COUNT(*) FROM dwd_order_detail_di "
                          "WHERE dt BETWEEN '2026-11-16' AND '2026-11-22' GROUP BY category"))
    assert ex["grain"] == "dim" and ex["dims"] == 1


def test_dynamic_examples_participate_in_pick(tmp_path, monkeypatch):
    path = tmp_path / "fewshot_dynamic.jsonl"
    path.write_text(json.dumps({
        "question": "按品类拆分支付订单数", "grain": "dim", "metric": "count", "dims": 1,
        "tables": ["dwd_order_detail_di"],
        "sql": "SELECT category, COUNT(*) FROM dwd_order_detail_di "
               "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY category",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fewshot, "_DYNAMIC_PATH", path)
    monkeypatch.setattr(fewshot, "_EXAMPLES", None)
    monkeypatch.setattr(fewshot, "_DYNAMIC_MTIME", None)

    from app.services.query_spec import QuerySpec
    spec = QuerySpec(metric="支付订单数", dimensions=["品类"])
    picked = fewshot.pick(spec, ["dwd_order_detail_di"], k=8)
    assert any(ex.get("question") == "按品类拆分支付订单数" for ex in picked)
