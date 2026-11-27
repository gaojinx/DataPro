"""👍 反馈候选池（D04）单测：去重写入、harvest 合并。"""
from eval.harvest import case_from_candidate, merge_pool
from app.services.feedback_pool import FeedbackPool

ENTRY = {"query_id": 7, "question": "查昨天的支付订单数",
         "question_context": None,
         "sql": "SELECT COUNT(DISTINCT order_id) FROM dwd_order_detail_di "
                "WHERE pay_status=1 AND dt BETWEEN '{date_start}' AND '{date_end}'"}


def test_capture_dedupes_by_query_id(tmp_path):
    pool = FeedbackPool(tmp_path / "pool.jsonl")
    assert pool.capture(ENTRY) is True
    assert pool.capture(ENTRY) is False            # 同一条反馈重复提交只保留一份
    assert len(pool.read_all()) == 1


def test_case_from_candidate_has_no_assertions():
    case = case_from_candidate(ENTRY)
    assert case["case_id"] == "P7"
    assert case["expect"] == {} and case["reviewed"] is False
    assert case["source"]["pool"] is True and case["source"]["volatile"] is True
    assert case["expected_tables"] == ["dwd_order_detail_di"]


def test_merge_pool_skips_duplicates():
    drafts = [case_from_candidate(ENTRY)]
    merged = merge_pool(drafts, [ENTRY])
    assert len(merged) == 1
    other = dict(ENTRY, query_id=8, question="查昨天的销售额")
    merged = merge_pool(merged, [other])
    assert len(merged) == 2 and merged[-1]["case_id"] == "P8"


def test_merge_pool_skips_incomplete_entries():
    assert merge_pool([], [{"query_id": 9, "question": "没有 SQL"}]) == []
