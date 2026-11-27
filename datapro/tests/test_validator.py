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


def test_group_by_identifier_rejected():
    # A07：按个体/单据标识分组等于返回明细，必须拦截
    for col in ("user_id", "order_id", "uid"):
        r = validate_sql(f"SELECT {col}, COUNT(*) FROM dwd_order_detail_di GROUP BY {col}", ALLOWED)
        assert not r.passed and "A07" in r.errors[-1]


def test_group_by_pii_rejected():
    r = validate_sql("SELECT phone, COUNT(*) FROM dwd_user_di GROUP BY phone", ALLOWED)
    assert not r.passed and "A07" in r.errors[-1]


def test_group_by_business_dimensions_allowed():
    # 渠道/品类/城市等业务维度不受影响；channel_id 是维表外键，正常放行
    for col in ("category", "channel", "channel_id", "dt"):
        r = validate_sql(f"SELECT {col}, COUNT(*) FROM dwd_order_detail_di GROUP BY {col}", ALLOWED)
        assert r.passed, (col, r.errors)


def test_forbidden_function():
    r = validate_sql("SELECT SLEEP(10)", ALLOWED)
    assert not r.passed


def test_pg_dialect_rejected():
    # PostgreSQL 专用函数与间隔写法在 MySQL 下不可执行（曾导致 E010）
    r = validate_sql(
        "SELECT category FROM dwd_order_detail_di "
        "WHERE dt >= DATE_TRUNC('week', CAST('2026-11-22' AS DATE)) "
        "AND dt < CAST('2026-11-22' AS DATE) + INTERVAL '1' DAY", ALLOWED)
    assert not r.passed
    assert any("date_trunc" in e or "INTERVAL" in e for e in r.errors)


def test_date_placeholders_pass():
    r = validate_sql(
        "SELECT category, COUNT(*) AS cnt FROM dwd_order_detail_di "
        "WHERE dt BETWEEN '{start_date}' AND '{end_date}' GROUP BY category", ALLOWED)
    assert r.passed


def test_existing_limit_preserved():
    r = validate_sql("SELECT * FROM dwd_user_di LIMIT 5", ALLOWED)
    assert r.passed and "LIMIT 5" in r.sql_final


def test_syntax_error():
    assert not validate_sql("SELEC * FROM dwd_user_di", ALLOWED).passed


def test_user_level_whitelist_intersection():
    # 用户级白名单取交集（权限设计 §1.2 备选方案）
    r = validate_sql("SELECT * FROM dwd_user_di", ALLOWED, user_tables={"dwd_order_detail_di"})
    assert not r.passed
