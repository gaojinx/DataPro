"""LLM 接入层单测（B09/B17）与执行引擎日期注入（B06）。"""
from datetime import date

from app.llm.client import _parse_json
from app.services.executor import inject_dates

TODAY = date(2026, 11, 21)


def test_parse_json_plain():
    assert _parse_json('{"sql": "SELECT 1"}')["sql"] == "SELECT 1"


def test_parse_json_markdown_fence():
    raw = '```json\n{"sql": "SELECT 2", "confidence": "high"}\n```'
    assert _parse_json(raw)["sql"] == "SELECT 2"


def test_parse_json_noise_around_object():
    raw = '好的，结果如下：{"sql": "SELECT 3"} 以上。'
    assert _parse_json(raw)["sql"] == "SELECT 3"


def test_parse_json_invalid_returns_none():
    assert _parse_json("这不是 JSON") is None
    assert _parse_json("") is None


def test_inject_dates_default_last_7_days():
    sql = "SELECT * FROM t WHERE dt >= '{start_date}' AND dt <= '{end_date}'"
    out = inject_dates(sql, today=TODAY)
    assert "2026-11-20" in out          # end_date = T-1
    assert "2026-11-14" in out          # start_date = T-7
    assert "{start_date}" not in out


def test_inject_dates_single_date():
    assert inject_dates("WHERE dt='{date}'", today=TODAY) == "WHERE dt='2026-11-20'"


def test_inject_dates_leaves_other_braces():
    sql = "SELECT '{not_a_date}'"
    assert inject_dates(sql, today=TODAY) == sql
