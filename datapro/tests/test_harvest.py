"""golden 评测集沉淀脚本测试（不连数据库、不调模型）。"""
from datetime import date

from app.core.time_window import TimeWindow
from eval import harvest


def test_infer_category():
    assert harvest.infer_category(
        "SELECT c.channel_name, COUNT(*) FROM dwd_shop.dwd_order_detail_di o "
        "JOIN dwd_shop.dim_channel c ON o.channel = c.channel_name GROUP BY c.channel_name"
    ) == "multi_join"
    assert harvest.infer_category(
        "SELECT dt, COUNT(*) FROM dwd_shop.dwd_order_detail_di "
        "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY dt"
    ) == "time_range"
    assert harvest.infer_category(
        "SELECT COUNT(*) FROM dwd_shop.dwd_order_detail_di WHERE pay_status = 1"
    ) == "simple_agg"


def test_infer_tables():
    assert harvest.infer_tables(
        "SELECT 1 FROM dwd_shop.dwd_order_detail_di o "
        "JOIN dwd_shop.dim_channel c ON o.channel = c.channel_name"
    ) == ["dim_channel", "dwd_order_detail_di"]


def test_classify_negative():
    assert harvest.classify_negative("支付订单数是多少", "结果不完整") == "completeness"
    assert harvest.classify_negative("这个口径不对", None) == "caliber"
    assert harvest.classify_negative("随便问问", None) == "other"


def test_build_case_explicit_window():
    window = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "最近7天")
    case = harvest.build_case(
        {"id": 110, "question": "2026-11-16到2026-11-22的支付订单数",
         "generated_sql": "SELECT COUNT(*) FROM dwd_shop.dwd_order_detail_di "
                          "WHERE dt BETWEEN '{date_start}' AND '{date_end}'",
         "row_count": 1, "trace_id": "t1", "user_feedback": "up"},
        window=window, scalar=301, suspect=None)
    assert case["case_id"] == "H110"
    assert case["expect"]["time_window"] == {"start": "2026-11-16", "end": "2026-11-22"}
    assert case["expect"]["scalar"] == 301
    assert case["reviewed"] is False
    assert case["source"]["volatile"] is False


def test_split_context_multi_turn():
    history, current = harvest.split_context(
        "查2026-11-20的支付订单数；本次补充（时间/条件优先于上句）：按品类拆分一下")
    assert history == ["查2026-11-20的支付订单数"]
    assert current == "按品类拆分一下"
    assert harvest.split_context("查昨天的支付订单数") == ([], "查昨天的支付订单数")


def test_split_context_prefers_recorded_context():
    """有 question_context 时用完整前序提问（含中间轮次），不再靠拼接串拆。"""
    history, current = harvest.split_context(
        "那11月16日到11月22日呢", '["查2026-11-20的支付订单数", "按品类拆分一下"]')
    assert history == ["查2026-11-20的支付订单数", "按品类拆分一下"]
    assert current == "那11月16日到11月22日呢"
    assert harvest.split_context("查昨天的支付订单数", None) == ([], "查昨天的支付订单数")


def test_build_case_uses_question_context():
    window = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "区间")
    case = harvest.build_case(
        {"id": 200, "question": "那11月16日到11月22日呢",
         "question_context": '["查2026-11-20的支付订单数", "按品类拆分一下"]',
         "generated_sql": "SELECT category, COUNT(DISTINCT order_id) FROM dwd_shop.dwd_order_detail_di "
                          "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY category",
         "row_count": 5, "trace_id": "t4", "user_feedback": None},
        window=window, scalar=None, suspect=None)
    assert case["history"] == ["查2026-11-20的支付订单数", "按品类拆分一下"]
    assert case["question"] == "那11月16日到11月22日呢"


def test_build_case_volatile_default_window():
    window = TimeWindow(date(2026, 11, 16), date(2026, 11, 22), "day", "默认", explicit=False)
    case = harvest.build_case(
        {"id": 7, "question": "支付订单数", "generated_sql": "SELECT COUNT(*) FROM dwd_shop.dwd_order_detail_di",
         "row_count": 1, "trace_id": "t2", "user_feedback": None},
        window=window, scalar=12, suspect=None)
    assert "time_window" not in case["expect"]      # 相对/默认窗不断言，避免日期漂移
    assert case["source"]["volatile"] is True
    assert "volatile" in case["notes"]


def test_build_case_relative_is_volatile():
    """相对时间（昨天）会随数据日期漂移，不做断言，只留 digest 供人工改固定日期。"""
    window = TimeWindow(date(2026, 11, 22), date(2026, 11, 22), "day", "昨天")
    case = harvest.build_case(
        {"id": 104, "question": "查昨天的支付订单数",
         "generated_sql": "SELECT COUNT(*) FROM dwd_shop.dwd_order_detail_di",
         "row_count": 1, "trace_id": "t3", "user_feedback": "up"},
        window=window, scalar=43, suspect=None)
    assert case["expect"] == {}
    assert case["source"]["volatile"] is True
    assert case["expected_result_digest"] == "43"


def test_promote_only_reviewed(tmp_path, monkeypatch):
    draft = tmp_path / "harvest_draft.jsonl"
    golden = tmp_path / "eval_golden.jsonl"
    monkeypatch.setattr(harvest, "DRAFT_PATH", draft)
    monkeypatch.setattr(harvest, "GOLDEN_PATH", golden)
    harvest._write_jsonl(draft, [
        {"case_id": "H1", "question": "已确认", "reviewed": True, "source": {"suspect": None}},
        {"case_id": "H2", "question": "未确认", "reviewed": False, "source": {}},
        {"case_id": "H3", "question": "可疑", "reviewed": True, "source": {"suspect": "sql invalid"}},
    ])
    result = harvest.promote()
    assert result["promoted"] == 1
    rows = harvest._read_jsonl(golden)
    assert [r["question"] for r in rows] == ["已确认"]
    assert rows[0]["case_id"] == "G001"
    # 再跑一次不重复并入
    assert harvest.promote()["promoted"] == 0
    # 第二次 promote 新条目：编号顺延，不撞 G001
    harvest._write_jsonl(draft, [
        {"case_id": "H9", "question": "后来确认", "reviewed": True, "source": {"suspect": None}},
    ])
    assert harvest.promote()["promoted"] == 1
    rows = harvest._read_jsonl(golden)
    assert [r["case_id"] for r in rows] == ["G001", "G002"]


def test_carry_review_keeps_review_state():
    """重跑 harvest 覆盖草稿时，人工复核结论与手工断言要保留。"""
    previous = [{
        "case_id": "H114", "question": "按品类拆分一下", "history": ["查2026-11-20的支付订单数"],
        "reviewed": True, "notes": "harvested from query_record#114 · 复核：单日窗口，补 min_rows",
        "expect": {"time_window": {"start": "2026-11-20", "end": "2026-11-20"}, "min_rows": 2},
    }, {
        "case_id": "H110", "question": "上个月的支付订单数", "history": [],
        "reviewed": False, "notes": "harvested from query_record#110 · 复核：上月无数据，不入库",
        "expect": {},
    }]
    drafts = [
        {"case_id": "H114", "question": "按品类拆分一下", "history": ["查2026-11-20的支付订单数"],
         "reviewed": False, "notes": "harvested from query_record#114",
         "expect": {"time_window": {"start": "2026-11-20", "end": "2026-11-20"}}},
        {"case_id": "H110", "question": "上个月的支付订单数", "history": [],
         "reviewed": False, "notes": "harvested from query_record#110", "expect": {}},
        {"case_id": "H200", "question": "新问题", "history": [],
         "reviewed": False, "notes": "harvested from query_record#200", "expect": {}},
    ]
    out = harvest.carry_review(previous, drafts)
    assert out[0]["reviewed"] is True                      # 复核结论保留
    assert out[0]["expect"]["min_rows"] == 2               # 手工断言保留
    assert "复核：单日窗口" in out[0]["notes"]              # 复核备注保留
    assert out[1]["reviewed"] is False                     # 驳回判断保留（理由随备注留档）
    assert "复核：上月无数据" in out[1]["notes"]
    assert out[2]["reviewed"] is False and "复核" not in out[2]["notes"]
    # 重复 carry 不叠加备注
    again = harvest.carry_review(out, [dict(c) for c in out])
    assert again[0]["notes"].count("复核：单日窗口") == 1
