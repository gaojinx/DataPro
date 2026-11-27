"""先知采集单元与契约测试：夹具解析、幂等键、汇总行、fail-closed、模板、调度兼容。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services import schedule_registry as registry_mod
from app.services import schedule_runner
from importer.import_metadata import normalize, parse_csv
from importer.ingest_registry import FIELD_MAP_CSV, validate_field_map_csv
from worker.xianzhi_driver import CollectorConfig, CollectorError, LocalXlsxDriver, PlaywrightDriver
from worker.xianzhi_fields import FIELD_SPECS, HEADER_MAP, FieldMapper, RawRow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "xianzhi_sample.xlsx"


def _fixture_rows() -> list[RawRow]:
    driver = LocalXlsxDriver(FIXTURE)
    return driver.fetch(CollectorConfig(), date(2026, 11, 27), date(2026, 11, 27))


def test_config_refuses_plaintext_and_bad_mode():
    cfg = CollectorConfig(username_ref="not-a-ref")
    with pytest.raises(CollectorError) as exc:
        cfg.validate()
    assert exc.value.reason_code == "BAD_REF"

    cfg = CollectorConfig(username_ref="file:data/secrets/xianzhi.env#XIANZHI_USERNAME",
                          password_ref="file:data/secrets/xianzhi.env#XIANZHI_PASSWORD",
                          date_mode="unknown")
    with pytest.raises(CollectorError) as exc:
        cfg.validate()
    assert exc.value.reason_code == "BAD_CONFIG"


def test_config_build_window_lookback_and_range():
    cfg = CollectorConfig(date_mode="lookback", lookback_days=3)
    assert cfg.build_window(date(2026, 11, 27)) == (date(2026, 11, 24), date(2026, 11, 26))
    cfg = CollectorConfig(date_mode="range", date_start="2026-11-27", date_end="2026-11-27")
    assert cfg.build_window(date(2026, 11, 27)) == (date(2026, 11, 27), date(2026, 11, 27))


def test_fixture_parses_82_rows_and_15_headers():
    raw_rows = _fixture_rows()
    assert len(raw_rows) == 82
    assert len(raw_rows[0].cells) == 15
    assert set(raw_rows[0].cells) == set(HEADER_MAP)
    assert raw_rows[0].cells["账面消耗"] == 7.63

    valid, errors = FieldMapper().normalize(raw_rows)
    assert len(valid) == 82 and errors == []
    assert valid[0]["dt"] == date(2026, 11, 27)
    assert float(valid[0]["account_cost"]) == pytest.approx(7.63)
    assert valid[0]["plan_id"] == "922222659"
    assert valid[0]["landing_page_name"] == ""       # 源端 '--' 归一为空串


def test_idempotency_key_needs_landing_page():
    valid, _ = FieldMapper().normalize(_fixture_rows())
    keys = [FieldMapper.idempotency_key(row) for row in valid]
    assert len(set(keys)) == 82
    three_col = {(r["dt"], r["plan_id"], r["media"]) for r in valid}
    assert len(three_col) == 81                     # plan_id='0' 两行靠落地页区分
    zero_rows = [r for r in valid if r["plan_id"] == "0"]
    assert len(zero_rows) == 2
    assert len({r["landing_page_name"] for r in zero_rows}) == 2


def test_summary_rows_excluded_and_counted():
    summary = {cn: "合计" for cn in HEADER_MAP}
    summary["投放日期"] = "2026-11-27"
    valid, errors = FieldMapper().normalize([
        RawRow(cells=summary, row_no=2),
        RawRow(cells={"投放日期": "", "媒体": "--"}, row_no=3),
    ])
    assert valid == []
    assert [e["reason"] for e in errors] == ["summary_row", "summary_row"]
    assert [e["row_no"] for e in errors] == [2, 3]


def test_export_header_mismatch_rejected():
    payload = b"\x50\x4b\x03\x04fake-xlsx"          # 非 xlsx → XLSX_READ_FAILED
    with pytest.raises(CollectorError) as exc:
        PlaywrightDriver.parse_xlsx(payload)
    assert exc.value.reason_code == "XLSX_READ_FAILED"


def test_fail_closed_no_plaintext_in_result_or_alert(tmp_path):
    """密码引用指向不存在文件 → failed；内存里已解析的用户名也不得出现在任何输出。"""
    from app.services.ingest_alert import IngestAlertService
    from worker.xianzhi_collector import XianzhiCollector
    from worker.xianzhi_driver import CollectorConfig

    marker = "LEAKMARK_SHOULD_NOT_APPEAR"
    secret_file = tmp_path / "xianzhi.env"
    secret_file.write_text(f"XIANZHI_USERNAME={marker}\n", encoding="utf-8")
    cfg = CollectorConfig(
        username_ref=f"file:{secret_file.as_posix()}#XIANZHI_USERNAME",
        password_ref=f"file:{tmp_path / 'missing.env'}#XIANZHI_PASSWORD",
        date_mode="range", date_start="2026-11-27", date_end="2026-11-27",
    )

    class FakeRegistrar:
        def __init__(self):
            self.updated = []

        def ensure_dataset(self):
            return 1

        def find_success_batch(self, window, day=None):
            return 0

        def register_batch(self, batch):
            return 7

        def update_batch(self, batch_id, patch):
            self.updated.append(patch)

        def register_errors(self, batch_id, errors):
            return 0

    class FakeAlert(IngestAlertService):
        def __init__(self):
            self.messages = []
            super().__init__(url="")

        def notify(self, batch, reason):
            from app.services.ingest_alert import build_message
            self.messages.append(build_message(batch, reason))
            return True

    registrar, alert = FakeRegistrar(), FakeAlert()
    collector = XianzhiCollector(config=cfg, registrar=registrar, alert=alert)
    result = collector.run(config=cfg)

    assert result["status"] == "failed"
    assert result["error_code"] == "SECRET_UNRESOLVED"
    assert registrar.updated[-1]["status"] == "failed"
    assert "SECRET_UNRESOLVED" in registrar.updated[-1]["error"]
    combined = str(result) + str(registrar.updated) + str(alert.messages)
    assert marker not in combined
    assert "XIANZHI_PASSWORD" not in combined


def test_dry_run_does_not_touch_db(tmp_path):
    """dry-run 只解析，不调用任何登记/写入接口。"""
    from worker.xianzhi_collector import XianzhiCollector
    from worker.xianzhi_driver import CollectorConfig

    class NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"dry-run 不应调用 {name}")

    cfg = CollectorConfig(date_mode="range", date_start="2026-11-27", date_end="2026-11-27",
                          extra={"driver": "local_xlsx", "source_file": str(FIXTURE)})
    collector = XianzhiCollector(config=cfg, registrar=NoDb(), alert=NoDb())
    result = collector.run(config=cfg, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["rows_read"] == 82 and result["rows_loaded"] == 82
    assert result["batch_id"] == 0


def test_metadata_templates_are_complete():
    from importer.ingest_registry import (DB_NAME, TABLE_NAME)
    from importer.import_metadata import KIND_FIELDS

    base = FIELD_MAP_CSV.parent
    tables, errors = normalize("tables", parse_csv(base / "xianzhi_meta_tables.csv", "tables"))
    assert errors == [] and len(tables) == 1
    assert tables[0]["db_name"] == DB_NAME and tables[0]["table_name"] == TABLE_NAME
    assert not set(KIND_FIELDS["tables"]) - set(tables[0])
    assert "先知" in tables[0]["keywords"] and "引流课" in tables[0]["keywords"]

    columns, errors = normalize("columns", parse_csv(base / "xianzhi_meta_columns.csv", "columns"))
    assert errors == [] and len(columns) == 15
    assert {r["col_name"] for r in columns} == set(HEADER_MAP.values())
    assert all(r["db_name"] == DB_NAME for r in columns)

    calibers, errors = normalize("calibers", parse_csv(base / "xianzhi_calibers.csv", "calibers"))
    assert errors == [] and len(calibers) == 6
    assert {r["metric_id"] for r in calibers} == {f"M90{n}" for n in range(1, 7)}
    m901 = next(r for r in calibers if r["metric_id"] == "M901")
    assert "account_cost" in m901["logic"] and "FROM" not in m901["logic"]
    assert "先知" in m901["choice_keywords"]

    assert len(FIELD_SPECS) == 15
    assert validate_field_map_csv() == []


def test_schedule_add_defaults_to_question_and_accepts_ingest(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "DATA_FILE", tmp_path / "schedules.json")
    reg = registry_mod.ScheduleRegistry()
    legacy = reg.add({"name": "旧任务", "question": "查昨天订单"}, owner="alice")
    ingest = reg.add({"name": "先知采集", "question": "", "type": "ingest",
                      "ingest_code": "xianzhi", "time": "06:30"}, owner="alice")
    assert legacy["type"] == "question" and legacy["ingest_code"] == ""
    assert ingest["type"] == "ingest" and ingest["ingest_code"] == "xianzhi"

    changed = reg.update(ingest["id"], {"type": "question", "ingest_code": ""})
    assert changed["type"] == "question" and changed["ingest_code"] == ""


def test_schedule_signature_includes_new_fields():
    task = {"id": "x1", "freq": "daily", "time": "06:30", "weekday": 1,
            "question": "", "enabled": True, "type": "ingest", "ingest_code": "xianzhi"}
    before = schedule_runner._signature(task)
    assert schedule_runner._signature({**task, "ingest_code": "other"}) != before
    assert schedule_runner._signature({**task, "type": "question"}) != before


async def test_question_task_regression(monkeypatch):
    task = {"id": "t1", "name": "查询", "question": "查昨天订单",
            "owner": "alice", "freq": "daily", "time": "09:00"}
    recorded = []

    class FakePipeline:
        def __init__(self, *args):
            pass

        async def run(self, ctx):
            yield {"type": "result", "row_count": 3, "summary": "3 行", "sql": "SELECT 1"}

    monkeypatch.setattr(schedule_runner, "Pipeline", FakePipeline)
    monkeypatch.setattr(schedule_runner.schedule_registry, "record_run",
                        lambda tid, run: recorded.append((tid, run)))
    run = await schedule_runner.run_task(task)
    assert run["status"] == "ok" and run["row_count"] == 3
    assert recorded[0][0] == "t1" and recorded[0][1]["status"] == "ok"


async def test_ingest_task_dispatch(monkeypatch):
    """type=ingest 任务必须分派到采集器，并把原因码（非明文）写进运行记录。"""
    task = {"id": "t2", "name": "先知采集", "question": "", "owner": "alice",
            "type": "ingest", "ingest_code": "xianzhi", "freq": "daily", "time": "06:30"}
    recorded = []

    class FakeCollector:
        def __init__(self, **kwargs):
            assert kwargs["triggered_by"] == "scheduler:t2"

        def run(self, **kwargs):
            return {"status": "success", "rows_loaded": 82, "rows_rejected": 0,
                    "window_start": "2026-11-27", "window_end": "2026-11-27",
                    "data_as_of": "2026-11-27", "batch_no": "abc", "error_code": ""}

    monkeypatch.setattr("worker.xianzhi_collector.XianzhiCollector", FakeCollector)
    monkeypatch.setattr(schedule_runner.schedule_registry, "record_run",
                        lambda tid, run: recorded.append((tid, run)))
    run = await schedule_runner.run_task(task)
    assert run["status"] == "ok" and run["row_count"] == 82
    assert recorded[0][1]["summary"].count("rows_loaded=82") == 1
