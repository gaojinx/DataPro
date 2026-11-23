"""SQL 校验器测试：覆盖写操作、越权表、危险函数、LIMIT 补齐。"""
from app.services.validator import validate_sql

ALLOWED = {"dwd_order_detail_di", "dwd_user_di", "dim_channel"}


def test_select_pass():
    r = validate_sql("SELECT COUNT(*) FROM dwd_order_detail_di WHERE dt='2026-11-19'", ALLOWED)
    assert r.passed and r.sql_final.endswith("LIMIT 200")


def test_write_rejected():
    for sql in ["UPDATE dwd_order_detail_di SET amount=0",
                "DELETE FROM dwd_order_detail_di",
                "DROP TABLE dwd_order_detail_di",
                "TRUNCATE dwd_order_detail_di"]:
        assert not validate_sql(sql, ALLOWED).passed


def test_table_not_in_whitelist():
    r = validate_sql("SELECT * FROM finance_settlement", ALLOWED)
    assert not r.passed and "finance_settlement" in r.errors[0]


def test_nested_subquery_bypass():
    sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM finance_settlement) a) b"
    r = validate_sql(sql, ALLOWED)
    assert not r.passed


def test_forbidden_function():
    r = validate_sql("SELECT SLEEP(10)", ALLOWED)
    assert not r.passed


def test_existing_limit_preserved():
    r = validate_sql("SELECT * FROM dwd_user_di LIMIT 5", ALLOWED)
    assert r.passed and "LIMIT 5" in r.sql_final


def test_syntax_error():
    assert not validate_sql("SELEC * FROM dwd_user_di", ALLOWED).passed


def test_user_level_whitelist_intersection():
    # 用户级白名单取交集（权限设计 §1.2 备选方案）
    r = validate_sql("SELECT * FROM dwd_user_di", ALLOWED, user_tables={"dwd_order_detail_di"})
    assert not r.passed
