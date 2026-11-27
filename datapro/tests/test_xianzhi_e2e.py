"""先知采集端到端（本地 sqlite 模拟真实库）：解析→映射→排除→幂等落表→血缘→召回前置元数据。

真实 MySQL 的 DDL/授权/writer 账号验证属于「待实跑」项；本用例用 sqlite 验证同样的
幂等键、批次去重与行数语义，确保无 DB 环境也能回归全链路。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import source_session
from app.db.models_source import (BaseSource, IngestBatch, IngestDataset, IngestFieldMap,
                                  IngestRowError, IngestSource)
from app.services.ingest_alert import IngestAlertService
from importer.ingest_registry import IngestRegistrar
from worker.xianzhi_collector import XianzhiCollector
from worker.xianzhi_driver import CollectorConfig
from worker.xianzhi_ods import OdsWriter

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "xianzhi_sample.xlsx"
WINDOW = (date(2026, 11, 27), date(2026, 11, 27))

# sqlite 版 DDL：列与 MySQL 模板一致（系统列用兼容类型），唯一键完全一样
_SQLITE_DDL = """
CREATE TABLE ods_xianzhi_info_analysis (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dt TEXT NOT NULL,
  account_name TEXT NOT NULL DEFAULT '',
  media TEXT NOT NULL DEFAULT '',
  plan_name TEXT NOT NULL DEFAULT '',
  landing_page_name TEXT NOT NULL DEFAULT '',
  plan_id TEXT NOT NULL DEFAULT '',
  impressions INTEGER,
  clicks INTEGER,
  account_cost NUMERIC(18,4),
  login_users INTEGER,
  lead_signups INTEGER,
  lead_signup_cost NUMERIC(18,4),
  grade1_signups INTEGER,
  grade2_signups INTEGER,
  grade3_signups INTEGER,
  ingest_batch_id INTEGER,
  source_url TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (dt, plan_id, media, landing_page_name)
)
"""


class SqliteOdsWriter(OdsWriter):
    """仅测试用：表名去掉 schema 前缀，走 sqlite 的 INSERT OR REPLACE 幂等分支。"""

    TABLE = "ods_xianzhi_info_analysis"


# sqlite 不会给 BigInteger 主键自动递增；这两张表按模型字段手工建（仅测试）
_SQLITE_BIGINT_TABLES = {
    "ingest_batch": """
        CREATE TABLE ingest_batch (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_no VARCHAR(32) NOT NULL UNIQUE,
          dataset_id INTEGER NOT NULL,
          source_code VARCHAR(32) NOT NULL DEFAULT '',
          db_name VARCHAR(64) NOT NULL DEFAULT '',
          table_name VARCHAR(128) NOT NULL DEFAULT '',
          load_mode VARCHAR(16) NOT NULL DEFAULT 'incremental',
          window_start DATE, window_end DATE,
          source_file VARCHAR(255), file_sha256 VARCHAR(64),
          rows_read INTEGER NOT NULL DEFAULT 0,
          rows_loaded INTEGER NOT NULL DEFAULT 0,
          rows_rejected INTEGER NOT NULL DEFAULT 0,
          status VARCHAR(16) NOT NULL DEFAULT 'running',
          error TEXT, triggered_by VARCHAR(64) NOT NULL DEFAULT '',
          started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at DATETIME, duration_ms INTEGER
        )
    """,
    "ingest_row_error": """
        CREATE TABLE ingest_row_error (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL, row_no INTEGER NOT NULL DEFAULT 0,
          raw_row TEXT, reason VARCHAR(255) NOT NULL DEFAULT '',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
}


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(_SQLITE_DDL))
    tables = [t for t in BaseSource.metadata.tables.values()
              if t.name not in _SQLITE_BIGINT_TABLES]
    BaseSource.metadata.create_all(engine, tables=tables)
    with engine.begin() as conn:
        for ddl in _SQLITE_BIGINT_TABLES.values():
            conn.execute(text(ddl))
    monkeypatch.setattr(source_session, "SourceSessionLocal", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


def _collector(engine, triggered_by: str = "manual:test") -> tuple[XianzhiCollector, CollectorConfig]:
    config = CollectorConfig(
        date_mode="range", date_start=WINDOW[0].isoformat(), date_end=WINDOW[1].isoformat(),
        extra={"driver": "local_xlsx", "source_file": str(FIXTURE)},
    )
    writer = SqliteOdsWriter(engine=engine)
    collector = XianzhiCollector(
        config=config, writer=writer, registrar=IngestRegistrar(owner="qa"),
        alert=IngestAlertService(url=""), triggered_by=triggered_by,
    )
    return collector, config


def test_e2e_82_rows_twice_idempotent(db_env):
    collector, config = _collector(db_env)
    first = collector.run(config=config, skip_existing=False)
    assert first["status"] == "success"
    assert first["rows_read"] == 82 and first["rows_loaded"] == 82 and first["rows_rejected"] == 0
    assert first["window_start"] == "2026-11-27" and first["window_end"] == "2026-11-27"
    assert first["data_as_of"] == "2026-11-27"

    with db_env.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM ods_xianzhi_info_analysis")).scalar()
    assert count == 82

    second = collector.run(config=config, skip_existing=False)   # 强制再走一次 upsert
    assert second["status"] == "success" and second["rows_loaded"] == 82
    with db_env.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM ods_xianzhi_info_analysis")).scalar()
        cost = conn.execute(text(
            "SELECT SUM(account_cost) FROM ods_xianzhi_info_analysis WHERE dt='2026-11-27'"
        )).scalar()
    assert count == 82                                          # upsert 幂等，未翻倍
    assert cost > 0


def test_e2e_lineage_registered_and_success_batch_skipped(db_env):
    collector, config = _collector(db_env)
    result = collector.run(config=config)
    assert result["status"] == "success"

    with source_session.SourceSessionLocal() as db:
        source = db.query(IngestSource).filter_by(code="xianzhi").one()
        assert source.kind == "scraper" and source.owner == "qa"
        dataset = db.query(IngestDataset).filter_by(source_code="xianzhi",
                                                    dataset_code="info_analysis").one()
        assert dataset.db_name == "dwd_shop"
        assert dataset.table_name == "ods_xianzhi_info_analysis"
        assert dataset.primary_key == "dt,plan_id,media,landing_page_name"
        assert dataset.incremental_col == "dt" and dataset.partition_col == "dt"
        assert dataset.meta_registered is True
        assert db.query(IngestFieldMap).filter_by(dataset_id=dataset.id).count() == 15
        batches = db.query(IngestBatch).filter_by(dataset_id=dataset.id).all()
        assert len(batches) == 1
        assert batches[0].status == "success" and batches[0].rows_loaded == 82
        assert batches[0].window_start == WINDOW[0] and batches[0].window_end == WINDOW[1]
        assert batches[0].file_sha256 and len(batches[0].file_sha256) == 64
        assert batches[0].triggered_by == "manual:test"
        assert db.query(IngestRowError).filter_by(batch_id=batches[0].id).count() == 0

    writer = SqliteOdsWriter(engine=db_env, dataset_id=dataset.id)
    assert writer.find_success_batch(WINDOW, datetime.now().date()) == batches[0].id

    # 同窗口当天已有 success 批次 → 直接跳过，不重复写
    again = collector.run(config=config, skip_existing=True)
    assert again["status"] == "skipped" and again["rows_loaded"] == 0
    with source_session.SourceSessionLocal() as db:
        assert db.query(IngestBatch).filter_by(dataset_id=dataset.id).count() == 1


def test_e2e_summary_row_written_to_error_table(db_env, monkeypatch):
    """构造含汇总行的导出：有效行数不变、汇总行进 ingest_row_error、计数进批次。"""
    from worker.xianzhi_driver import RawRow

    from worker.xianzhi_driver import LocalXlsxDriver

    original_fetch = LocalXlsxDriver.fetch

    def fetch_with_summary(self, config, start, end, secrets=None):
        rows = original_fetch(self, config, start, end, secrets)
        cells = dict(rows[0].cells)
        cells["投放账户名称"] = "合计"
        return [RawRow(cells=cells, row_no=2), *rows]

    monkeypatch.setattr(LocalXlsxDriver, "fetch", fetch_with_summary)
    collector, config = _collector(db_env)
    result = collector.run(config=config)
    assert result["status"] == "success"
    assert result["rows_read"] == 83 and result["rows_loaded"] == 82
    assert result["rows_rejected"] == 1
    with source_session.SourceSessionLocal() as db:
        err = db.query(IngestRowError).one()
        assert err.reason == "summary_row" and "合计" in err.raw_row
