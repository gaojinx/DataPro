"""先知采集编排：凭据解析 → 抓数 → 清洗 → 幂等落表 → 血缘 → 告警。

入口：XianzhiCollector.run(config, window, dry_run)。
不 import app.services.file_ingest（会话绑定表与本链路无关）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any

from app.services.ingest_alert import IngestAlertService
from importer.ingest_registry import DB_NAME, SOURCE_CODE, TABLE_NAME, IngestRegistrar
from worker.xianzhi_driver import (CollectorConfig, CollectorDriver, CollectorError,
                                   LocalXlsxDriver, build_driver, report_source_url,
                                   resolve_secrets)
from worker.xianzhi_fields import FieldMapper
from worker.xianzhi_ods import OdsWriter

logger = logging.getLogger("datapro.ingest.collector")

MAX_REJECT_RATIO = 0.05


class XianzhiCollector:
    """一次运行的编排器；依赖可注入，便于夹具/单测替换 driver/writer/registrar。"""

    def __init__(self, config: CollectorConfig | None = None, driver: CollectorDriver | None = None,
                 writer: OdsWriter | None = None, registrar: IngestRegistrar | None = None,
                 alert: IngestAlertService | None = None,
                 triggered_by: str = "manual:cli") -> None:
        self.config = config or CollectorConfig()
        self.driver = driver
        self.writer = writer
        self.registrar = registrar or IngestRegistrar()
        self.alert = alert or IngestAlertService()
        self.triggered_by = triggered_by
        self.mapper = FieldMapper()
        self._owns_driver = False

    def _resolve_secrets(self, config: CollectorConfig) -> dict[str, str]:
        """本地 xlsx 回填不需要站点凭据；其余驱动一律 fail-closed。"""
        if config.extra.get("driver") == "local_xlsx":
            config.validate(require_secrets=False)
            return {}
        return resolve_secrets(config)

    def _build_summary(self, config: CollectorConfig, window: tuple[date, date],
                       batch_no: str, started: datetime) -> dict[str, Any]:
        return {
            "batch_no": batch_no,
            "source_code": config.source_code,
            "db_name": DB_NAME,
            "table_name": TABLE_NAME,
            "window_start": window[0].isoformat(),
            "window_end": window[1].isoformat(),
            "rows_read": 0,
            "rows_loaded": 0,
            "rows_rejected": 0,
            "status": "running",
            "error_code": "",
            "data_as_of": window[1].isoformat(),
            "triggered_by": self.triggered_by,
            "batch_id": 0,
            "duration_ms": 0,
        }

    def _finish(self, summary: dict[str, Any], status: str, error_code: str = "",
                started: datetime | None = None) -> dict[str, Any]:
        summary["status"] = status
        summary["error_code"] = error_code
        if started is not None:
            summary["duration_ms"] = int((datetime.now() - started).total_seconds() * 1000)
        return summary

    def run(self, config: CollectorConfig | None = None,
            window: tuple[date, date] | None = None,
            dry_run: bool = False, skip_existing: bool = True,
            local_file: str = "") -> dict[str, Any]:
        """执行一批采集。dry_run 不写任何库（含血缘库）；失败返回 failed 字典而不抛异常。"""
        cfg = config or self.config
        if local_file:
            cfg.extra = {**cfg.extra, "driver": "local_xlsx", "source_file": local_file}
            self.driver = LocalXlsxDriver(local_file)
        started = datetime.now()
        batch_no = uuid.uuid4().hex
        try:
            active_window = window or cfg.build_window(started.date())
        except CollectorError as exc:
            return self._finish(self._build_summary(cfg, (started.date(), started.date()),
                                                    batch_no, started), "failed", exc.reason_code)
        summary = self._build_summary(cfg, active_window, batch_no, started)
        source_url = report_source_url(cfg)
        driver = self.driver or build_driver(cfg)
        try:
            if dry_run:
                secrets = self._resolve_secrets(cfg)
                raw_rows = driver.fetch(cfg, *active_window, secrets)
                valid, errors = self.mapper.normalize(raw_rows)
                summary["rows_read"] = len(raw_rows)
                summary["rows_rejected"] = len(errors)
                summary["rows_loaded"] = len(valid)
                return self._finish(summary, "dry_run")

            dataset_id = self.registrar.ensure_dataset()
            if skip_existing:
                existing = self.registrar.find_success_batch(active_window, started.date())
                if existing:
                    summary["batch_id"] = int(existing)
                    summary["rows_loaded"] = 0
                    return self._finish(summary, "skipped")

            # 先落 running 批次：凭据解析失败时也要留下 failed 血缘（只写引用名，不写值）。
            summary["batch_id"] = self.registrar.register_batch({
                "batch_no": batch_no,
                "dataset_id": dataset_id,
                "source_code": SOURCE_CODE,
                "db_name": DB_NAME,
                "table_name": TABLE_NAME,
                "load_mode": "incremental",
                "window_start": active_window[0],
                "window_end": active_window[1],
                "source_file": str(cfg.extra.get("source_file") or source_url),
                "file_sha256": getattr(driver, "sha256", "") or "",
                "rows_read": 0,
                "rows_loaded": 0,
                "rows_rejected": 0,
                "status": "running",
                "triggered_by": self.triggered_by,
                "started_at": started,
            })

            secrets = self._resolve_secrets(cfg)
            if self.writer is None:
                self.writer = OdsWriter(dataset_id=dataset_id)
            else:
                self.writer.dataset_id = dataset_id
            raw_rows = driver.fetch(cfg, *active_window, secrets)
            valid, errors = self.mapper.normalize(raw_rows)
            summary["rows_read"] = len(raw_rows)
            summary["rows_rejected"] = len(errors)
            summary["file_sha256"] = getattr(driver, "sha256", "") or ""

            if not valid or len(errors) > max(1, len(raw_rows) * MAX_REJECT_RATIO):
                self.registrar.register_errors(summary["batch_id"], errors)
                self.registrar.update_batch(summary["batch_id"], {
                    "rows_read": summary["rows_read"], "rows_rejected": summary["rows_rejected"],
                    "file_sha256": summary["file_sha256"],
                    "status": "failed", "error": "ROWS_REJECTED",
                    "finished_at": datetime.now(), "duration_ms": summary["duration_ms"],
                })
                self.alert.notify(summary, "ROWS_REJECTED")
                return self._finish(summary, "failed", "ROWS_REJECTED")

            summary["rows_loaded"] = self.writer.upsert(valid, summary["batch_id"], source_url)
            self.registrar.register_errors(summary["batch_id"], errors)
            self.registrar.update_batch(summary["batch_id"], {
                "rows_read": summary["rows_read"], "rows_loaded": summary["rows_loaded"],
                "rows_rejected": summary["rows_rejected"],
                "file_sha256": summary["file_sha256"], "status": "success",
                "error": "", "finished_at": datetime.now(),
                "duration_ms": summary["duration_ms"],
            })
            return self._finish(summary, "success")
        except CollectorError as exc:
            return self._fail(summary, started, exc.reason_code)
        except Exception as exc:  # noqa: BLE001 —— 只透出类型名，绝不回显异常原文/HTML
            logger.warning("采集异常：%s", type(exc).__name__)
            return self._fail(summary, started, f"UNEXPECTED:{type(exc).__name__}")
        finally:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass

    def _fail(self, summary: dict[str, Any], started: datetime, code: str) -> dict[str, Any]:
        """统一失败收口：批次 failed + 告警，error 只写原因码。"""
        self._finish(summary, "failed", code, started)
        if summary.get("batch_id"):
            try:
                self.registrar.update_batch(summary["batch_id"], {
                    "rows_read": summary["rows_read"], "rows_loaded": summary["rows_loaded"],
                    "rows_rejected": summary["rows_rejected"],
                    "file_sha256": summary.get("file_sha256", ""),
                    "status": "failed", "error": code, "finished_at": datetime.now(),
                    "duration_ms": summary["duration_ms"],
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("批次状态更新失败：%s", type(exc).__name__)
        try:
            self.alert.notify(summary, code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警发送失败：%s", type(exc).__name__)
        return summary
