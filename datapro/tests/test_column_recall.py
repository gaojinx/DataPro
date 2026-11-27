"""字段级检索（二级字段文档）：文档构造、关键词命中、表补位、prompt 前置。

核心约束：字段文档与表文档共用同一套文本构造函数；字段命中不能改动全局元数据缓存。
"""
import asyncio

from app.services.recall import _cn_forms, _column_hits, _derived_column_hits
from app.services.vector_recall import column_doc_text, flatten_columns


def _table(name="dim_goods", cn="商品维表", columns=None):
    columns = columns or [
        {"name": "goods_id", "cn_name": "商品编号", "type": "varchar(8)"},
        {"name": "supplier", "cn_name": "供应商", "type": "varchar(32)",
         "pitfall": "可能有多个供应商，需去重"},
        {"name": "unit_price", "cn_name": "标准单价", "type": "decimal(10,2)"},
    ]
    return {"table_name": name, "cn_name": cn, "description": "商品维度",
            "columns": [c["name"] for c in columns], "column_meta": columns}


def test_cn_forms_keep_core_word_and_drop_generic_suffix():
    assert "预算" in _cn_forms("计划预算")          # 中心语在后
    assert _cn_forms("商品名称") == ["商品名称"]     # 「名称」是通用后缀，不能单独当信号
    assert _cn_forms("支付状态") == ["支付状态"]


def test_column_hits_matches_full_name_and_ascii_whole_word():
    tables = [_table()]
    assert _column_hits("按供应商看销售额", tables) == {"dim_goods": ["supplier"]}
    # 英文按整词匹配：amount 不该命中 total_amount 这类词
    assert _column_hits("total_amount 是多少", tables) == {}


def test_column_hits_ignores_short_english_names():
    tables = [_table(columns=[{"name": "dt", "cn_name": "分区日期"}])]
    # 「dt」太短，整词命中也不采信；中文名仍然可以命中
    assert _column_hits("按 dt 看", tables) == {}
    assert _column_hits("按分区日期看", tables) == {"dim_goods": ["dt"]}


def test_derived_hits_cover_core_word_but_never_add_tables():
    """中心语命中只做提示：它可能是碎片词，绝不能用来补表。"""
    tables = [_table()]
    assert _derived_column_hits("各商品的单价", tables) == {"dim_goods": ["unit_price"]}
    # 「工单数量」里的「单数」是「支付订单数」的碎片：这类命中只允许出现在字段提示里，
    # 绝不能补表（补表用的是 _column_hits，它要求全名命中，这里必须为空）
    order_table = {"table_name": "dws_category_daily", "cn_name": "品类日汇总",
                   "columns": ["order_cnt"], "column_meta": [
                       {"name": "order_cnt", "cn_name": "支付订单数"}]}
    q = "昨天的工单数量统计"
    assert _column_hits(q, [order_table]) == {}


def test_column_hits_keeps_only_longest_match_in_a_table():
    """「支付订单数」与「退款订单数」都能命中时，只留匹配更长的那个。"""
    t = {"table_name": "dws_category_daily", "cn_name": "品类日汇总",
         "columns": ["order_cnt", "refund_cnt"], "column_meta": [
             {"name": "order_cnt", "cn_name": "支付订单数"},
             {"name": "refund_cnt", "cn_name": "退款订单数"}]}
    assert _derived_column_hits("支付订单数是多少", [t]) == {"dws_category_daily": ["order_cnt"]}


def test_flatten_columns_carries_table_context_and_skips_blank():
    cols = flatten_columns([_table(), {"table_name": "t2", "column_meta": [{"cn_name": "无名列"}]}])
    assert [c["name"] for c in cols] == ["goods_id", "supplier", "unit_price"]
    assert cols[0]["table_name"] == "dim_goods" and cols[0]["table_cn"] == "商品维表"


def test_column_doc_text_has_no_sample_values_or_pii():
    cm = {**flatten_columns([_table()])[1], "sample": "某某供应商", "pii": True}
    text = column_doc_text(cm)
    assert "dim_goods" in text and "供应商" in text and "varchar(32)" in text
    assert "去重" in text                     # 业务提示要进文本，它常是唯一线索
    assert "某某供应商" not in text            # 样例值会随编码落到向量库，不能带


def test_build_docs_emits_secondary_column_documents():
    from importer.export_vector_docs import build_docs

    docs = build_docs([_table()], [], [])
    uids = [d["uid"] for d in docs]
    assert uids[0] == "table:dim_goods"
    assert uids[1:] == ["column:dim_goods.goods_id", "column:dim_goods.supplier",
                        "column:dim_goods.unit_price"]
    assert all(d["type"] == "column" for d in docs[1:])
    assert docs[1]["payload"]["table_name"] == "dim_goods"


def test_recall_adds_table_by_column_hit_without_polluting_cache(monkeypatch):
    """字段命中能把表补进召回，且不能把 hit_columns 写回全局元数据对象。"""
    from app.services import recall as recall_mod

    tables = [
        _table(),
        {"table_name": "dwd_order_detail_di", "cn_name": "订单明细", "description": "订单",
         "columns": ["order_id", "dt"],
         "column_meta": [{"name": "order_id", "cn_name": "订单编号"},
                         {"name": "dt", "cn_name": "分区日期"}]},
    ]
    monkeypatch.setattr(recall_mod.metadata_store, "tables", lambda: tables)
    monkeypatch.setattr(recall_mod.metadata_store, "calibers", lambda: [])

    result = asyncio.run(recall_mod.recall_service.recall("按供应商看金额"))
    names = [t["table_name"] for t in result.tables]
    assert "dim_goods" in names
    assert result.column_hits == {"dim_goods": ["supplier"]}
    goods = next(t for t in result.tables if t["table_name"] == "dim_goods")
    assert goods["hit_columns"] == ["supplier"]
    # 全局缓存对象必须干净，否则下一个请求会继承上一个的命中字段
    assert "hit_columns" not in tables[0]


def test_schema_block_puts_hit_columns_first():
    from app.core.statemachine import PipelineContext
    from app.services.generator import _schema_block

    ctx = PipelineContext(question="按供应商看销售额", user_id="u")
    t = _table()
    ctx.recalled_schemas = [{**t, "schema": "dwd_shop", "hit_columns": ["supplier"]}]
    block = _schema_block(ctx)
    assert "问题中提到的字段" in block and "supplier" in block
    # 命中字段排在该表字段列表最前
    line = next(l for l in block.splitlines() if "字段:" in l)
    assert line.index("supplier（供应商）") < line.index("goods_id（商品编号）")
