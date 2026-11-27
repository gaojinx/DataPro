"""元数据导入（D05）单测：解析、校验、合并、向量文档导出，不依赖数据库。"""
from importer.export_vector_docs import build_docs
from importer.import_metadata import normalize, parse_csv
from app.services.metadata_store import merge_calibers, merge_tables


def test_parse_and_normalize_tables(tmp_path):
    f = tmp_path / "tables.csv"
    f.write_text("库名,表名,中文业务名,业务场景说明\n"
                 "dwd_shop,dwd_refund_di,退款明细日报表,一行=一笔退款\n",
                 encoding="utf-8-sig")
    rows = normalize("tables", parse_csv(f, "tables"))[0]
    assert rows[0]["db_name"] == "dwd_shop"
    assert rows[0]["table_name"] == "dwd_refund_di"
    assert rows[0]["description"].startswith("一行=一笔退款")


def test_normalize_columns_and_pii_flag():
    rows = [{"table_name": "dwd_user_di", "col_name": "phone", "中文名": "手机号", "是否敏感": "是"},
            {"table_name": "dwd_user_di"}]
    valid, errors = normalize("columns", rows)
    assert len(valid) == 1 and valid[0]["col_name"] == "phone"
    assert valid[0]["is_pii"] is True          # 中文表头别名与布尔归一化
    assert errors and "col_name" in errors[0]


def test_normalize_calibers_splits_aliases():
    valid, _ = normalize("calibers", [{
        "metric_id": "M010", "metric_name": "退款金额", "definition": "退款订单金额合计",
        "logic": "SUM(amount) WHERE pay_status=2",
        "aliases": "退款金额，退单金额", "choice_keywords": "退款,退货"}])
    assert valid[0]["aliases"] == "退款金额,退单金额"
    assert valid[0]["choice_keywords"] == "退款,退货"


def test_db_rows_override_seed_rows():
    seed_t = [{"table_name": "t1", "description": "种子"}]
    db_t = [{"table_name": "t1", "description": "库表"}, {"table_name": "t2", "description": "新增"}]
    merged = {t["table_name"]: t for t in merge_tables(db_t, seed_t)}
    assert merged["t1"]["description"] == "库表" and "t2" in merged

    seed_c = [{"metric_id": "M003", "metric_name": "支付订单数", "choice_keywords": ["支付"]}]
    db_c = [{"metric_id": "M003", "metric_name": "支付订单数", "definition": "库里的定义"}]
    merged_c = merge_calibers(db_c, seed_c)
    assert merged_c[0]["definition"] == "库里的定义"
    assert merged_c[0]["choice_keywords"] == ["支付"]   # 库表缺的字段沿用种子


def test_export_vector_docs_shapes():
    tables = [{"table_name": "dwd_refund_di", "cn_name": "退款明细日报表",
               "description": "退款、退货", "keywords": ["退款"], "columns": ["id", "amount"]}]
    calibers = [{"metric_id": "M010", "metric_name": "退款金额",
                 "aliases": ["退单金额"], "definition": "退款订单金额合计"}]
    knowledge = [{"id": "K1", "term": "完播率", "aliases": ["完整播放率"], "definition": "看完的比例"}]
    docs = build_docs(tables, calibers, knowledge)
    assert [d["uid"] for d in docs] == ["table:dwd_refund_di", "caliber:M010", "knowledge:K1"]
    assert docs[0]["type"] == "table" and "退款" in docs[0]["text"]
    assert "退单金额" in docs[1]["text"] and docs[1]["payload"]["metric_id"] == "M010"
    assert docs[2]["type"] == "knowledge" and "完播率" in docs[2]["text"]
