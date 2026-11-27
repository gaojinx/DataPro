"""IR 一致性校验测试（规则版，不依赖模型）。"""
from app.services.query_spec import QuerySpec
from app.services.spec_check import check

GROUPED = ("SELECT category, COUNT(*) AS cnt FROM dwd_shop.dwd_order_detail_di "
           "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY category LIMIT 200")
TOTAL = ("SELECT COUNT(*) AS cnt FROM dwd_shop.dwd_order_detail_di "
         "WHERE dt BETWEEN '{date_start}' AND '{date_end}' LIMIT 200")


def _spec(**kw) -> QuerySpec:
    base = dict(metric="支付订单数", time_phrase="最近7天", grain="total",
                dimensions=[], source="llm")
    base.update(kw)
    return QuerySpec(**base)


def test_total_but_grouped_is_flagged():
    issues = check(_spec(), GROUPED, "上周支付订单数是多少")
    assert issues and "GROUP BY" in issues[0]


def test_total_with_dimension_hint_is_not_flagged():
    """问题里明显要分组（各/每/按），说明是 spec 漏抽维度，不应回炉。"""
    assert check(_spec(), GROUPED, "各渠道的支付订单数") == []


def test_dimension_without_group_by_is_flagged():
    issues = check(_spec(grain="品类", dimensions=["品类"]), TOTAL, "上周每个品类的支付订单数")
    assert issues and "GROUP BY" in issues[0]


def test_dimension_with_group_by_ok():
    assert check(_spec(grain="品类", dimensions=["品类"]), GROUPED, "上周每个品类的支付订单数") == []


def test_rule_source_not_flagged():
    """规则抽取的 spec 本身可能漏维度，不据此否定 SQL。"""
    assert check(_spec(source="rule"), GROUPED, "上周支付订单数") == []


def test_hardcoded_date_is_flagged():
    sql = "SELECT COUNT(*) FROM dwd_shop.dwd_order_detail_di WHERE dt BETWEEN '2026-11-01' AND '2026-11-07'"
    issues = check(_spec(), sql, "上周支付订单数")
    assert issues and "占位符" in issues[0]


def test_placeholder_ok():
    assert check(_spec(), TOTAL, "上周支付订单数") == []


def test_subquery_group_by_ok():
    sql = ("SELECT COUNT(*) AS cnt FROM (SELECT category, COUNT(*) FROM dwd_shop.dwd_order_detail_di "
           "WHERE dt BETWEEN '{date_start}' AND '{date_end}' GROUP BY category) t")
    assert check(_spec(), sql, "上周支付订单数") == []


def test_detail_request_not_treated_as_total():
    """「退款的数据给我看看」是明细诉求，不能用 total 规则回炉。"""
    assert check(_spec(metric="退款"), GROUPED, "退款的数据给我看看") == []


def test_uncertain_metric_skips_total_rule():
    assert check(_spec(ambiguous=["metric"]), GROUPED, "退款的情况") == []


def test_hallucinated_dimension_ignored():
    """spec 凭空多出的维度（问题里没有）不算约束，避免误伤正常答案。"""
    assert check(_spec(grain="城市", dimensions=["城市"]), TOTAL, "上周支付订单数") == []


def test_parse_error_ignored():
    assert check(_spec(), "SELECT FROM WHERE", "上周支付订单数") == []
