"""「一份文档的文本」只能有一个定义：内存索引与 Milvus 灌库必须完全一致。

两个后端各写一套文本时不会报错，只会让召回率随部署形态漂移，线上极难发现。
"""
from importer.export_vector_docs import build_docs
from app.services.vector_recall import (caliber_doc_text, knowledge_doc_text,
                                        table_doc_text)


def _table():
    return {"table_name": "dwd_order_detail_di", "cn_name": "订单明细日报表",
            "description": "一行=一笔订单一个商品", "keywords": ["订单", "GMV"],
            "columns": ["order_id", "amount", "dt"]}


def _caliber():
    return {"metric_id": "M003", "metric_name": "支付订单数",
            "aliases": ["成交订单数"], "definition": "支付成功且未退款的订单去重数"}


def test_export_docs_reuse_the_same_text_as_memory_index():
    docs = build_docs([_table()], [_caliber()], [])
    assert docs[0]["text"] == table_doc_text(_table())
    assert docs[1]["text"] == caliber_doc_text(_caliber())


def test_table_text_carries_keywords_and_columns():
    text = table_doc_text(_table())
    assert "订单明细日报表" in text
    assert "GMV" in text            # 关键词必须进文本，否则口语别名召回不到
    assert "order_id" in text       # 字段进文本，模型与索引才看得到可查列


def test_caliber_text_carries_aliases():
    assert "成交订单数" in caliber_doc_text(_caliber())


def test_knowledge_text_carries_formula():
    assert "a/b" in knowledge_doc_text({"term": "完播率", "definition": "比例",
                                       "formula": "a/b"})


def test_wide_table_columns_are_capped_and_placed_last():
    """超宽表只放前 30 列且排在最后：编码窗口截尾时先丢字段，不丢描述与关键词。"""
    wide = {**_table(), "columns": [f"c{i}" for i in range(80)]}
    text = table_doc_text(wide)
    assert "c29" in text and "c30" not in text
    assert text.index("一行=一笔订单一个商品") < text.index("字段：")
