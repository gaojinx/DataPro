"""few-shot 按形状检索测试（不依赖模型）。"""
from app.services import fewshot
from app.services.query_spec import QuerySpec


def test_pick_matches_shape():
    total = QuerySpec(metric="支付订单数", grain="total")
    picked = fewshot.pick(total, ["dwd_order_detail_di"], k=2)
    assert picked and picked[0]["grain"] == "total"
    assert picked[0]["metric"] == "count"

    dim = QuerySpec(metric="销量", grain="品类", dimensions=["品类"])
    assert fewshot.pick(dim, ["dwd_order_detail_di"], k=2)[0]["grain"] == "dim"


def test_pick_join_example_for_multi_table():
    spec = QuerySpec(metric="支付订单数", grain="渠道", dimensions=["渠道"])
    picked = fewshot.pick(spec, ["dwd_order_detail_di", "dim_channel"], k=2)
    assert any(len(e["tables"]) > 1 for e in picked)


def test_render_only_uses_placeholders():
    text = fewshot.render(QuerySpec(metric="销售额", grain="total"), ["dwd_order_detail_di"])
    assert "SELECT" in text and "{date_start}" in text
    assert "CURDATE" not in text


def test_metric_kind_mapping():
    assert fewshot._metric_kind(QuerySpec(metric="销售额")) == "sum"
    assert fewshot._metric_kind(QuerySpec(metric="客单价")) == "avg"
    assert fewshot._metric_kind(QuerySpec(metric="支付订单数")) == "count"
