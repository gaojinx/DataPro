"""召回评测（A05）单测：命中率与 miss 统计，使用假召回函数。"""
from eval.recall_eval import evaluate


CASES = [
    {"case_id": "C1", "category": "simple_agg", "question": "销售额",
     "expected_tables": ["dwd_order_detail_di"], "expected_calibers": []},
    {"case_id": "C2", "category": "simple_agg", "question": "支付订单数",
     "expected_tables": ["dwd_order_detail_di"], "expected_calibers": ["M003"]},
    {"case_id": "C3", "category": "metadata_qa", "question": "口径",
     "expected_tables": [], "expected_calibers": ["M001"]},
]


def _fake(question):
    if "销售额" in question or "支付" in question:
        return ([{"table_name": "dwd_order_detail_di"}],
                [{"metric_id": "M003"}] if "支付" in question else [])
    return ([], [])


def test_table_and_caliber_rates():
    r = evaluate(CASES, _fake)
    assert r["table_cases"] == 2 and r["table_hits"] == 2 and r["table_hit_rate"] == 1.0
    assert r["caliber_cases"] == 2 and r["caliber_hits"] == 1 and r["caliber_hit_rate"] == 0.5
    assert [m["case_id"] for m in r["misses"]] == ["C3"]


def test_category_breakdown():
    r = evaluate(CASES, _fake)
    assert r["by_category"]["simple_agg"]["table_cases"] == 2
    assert r["by_category"]["metadata_qa"]["caliber_cases"] == 1
