"""ingest_* 血缘登记：复用 app.db.source_session 的独立 engine，不改 5 张模型。

用法：
    python -m importer.ingest_registry init     # 登记 source/dataset/field_map（幂等）
    python -m importer.ingest_registry info     # 打印数据集登记摘要（不含凭据）
运行时由 worker.ingest_cli / XianzhiCollector 调用 register_batch/update_batch/register_errors。
"""
from __future__ import annotations

import csv
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.db import source_session
from app.db.models_source import (IngestBatch, IngestDataset, IngestFieldMap,
                                  IngestRowError, IngestSource)
from app.services.ingest_alert import sanitize_text
from worker.xianzhi_fields import FIELD_SPECS

SOURCE_CODE = "xianzhi"
DATASET_CODE = "info_analysis"
DB_NAME = "dwd_shop"
TABLE_NAME = "ods_xianzhi_info_analysis"
PRIMARY_KEY = "dt,plan_id,media,landing_page_name"
GRAIN = "一行=某投放日期×某计划ID×某媒体×某落地页的投放表现"
SCHEMA_VERSION = 1
MAX_ROW_ERRORS = 50

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
FIELD_MAP_CSV = TEMPLATE_DIR / "xianzhi_field_map.csv"


class IngestRegistrar:
    """数据源/数据集/字段映射/批次/坏行的登记入口（独立数据源库）。"""

    def __init__(self, owner: str = "") -> None:
        self.owner = owner or os.environ.get("XIANZHI_OWNER", "")

    @staticmethod
    def _upsert_source(db: Any) -> int:
        source = db.query(IngestSource).filter_by(code=SOURCE_CODE).one_or_none()
        if source is None:
            source = IngestSource(code=SOURCE_CODE, name="先知-代理商系统", kind="scraper",
                                  status="active")
            db.add(source)
            db.flush()
        else:
            source.name = "先知-代理商系统"
            source.kind = "scraper"
            source.status = "active"
        return int(source.id)

    @staticmethod
    def _upsert_dataset(db: Any, source_id: int) -> int:
        payload = {
            "db_name": DB_NAME,
            "table_name": TABLE_NAME,
            "cn_name": "先知代理商投放明细日表",
            "grain": GRAIN,
            "primary_key": PRIMARY_KEY,
            "incremental_col": "dt",
            "partition_col": "dt",
            "load_mode": "incremental",
            "dtype_policy": "typed",
            "schema_version": SCHEMA_VERSION,
            "status": "active",
            "meta_registered": True,
        }
        dataset = db.query(IngestDataset).filter_by(
            source_code=SOURCE_CODE, dataset_code=DATASET_CODE).one_or_none()
        if dataset is None:
            # 必填列必须在 add/flush 前给齐：flush 会立刻发 INSERT，晚赋值会撞 NOT NULL。
            dataset = IngestDataset(source_code=SOURCE_CODE, dataset_code=DATASET_CODE, **payload)
            db.add(dataset)
        else:
            for key, value in payload.items():
                setattr(dataset, key, value)
        db.flush()
        return int(dataset.id)

    @staticmethod
    def _upsert_field_map(db: Any, dataset_id: int) -> int:
        count = 0
        for src_field, col_name, data_type, transform, required, comment in FIELD_SPECS:
            row = db.query(IngestFieldMap).filter_by(
                dataset_id=dataset_id, schema_version=SCHEMA_VERSION, col_name=col_name).one_or_none()
            if row is None:
                row = IngestFieldMap(dataset_id=dataset_id, schema_version=SCHEMA_VERSION,
                                     col_name=col_name)
                db.add(row)
                count += 1
            row.src_field = src_field
            row.data_type = data_type
            row.transform = transform
            row.required = bool(required)
            row.comment = comment
        return count

    def register_all(self) -> dict[str, int]:
        """幂等登记来源、数据集与 15 行字段映射，返回各表写入数量。"""
        with source_session.SourceSessionLocal() as db:
            source_id = self._upsert_source(db)
            source = db.query(IngestSource).filter_by(id=source_id).one()
            source.owner = self.owner
            dataset_id = self._upsert_dataset(db, source_id)
            added = self._upsert_field_map(db, dataset_id)
            db.commit()
        return {"source_id": source_id, "dataset_id": dataset_id, "field_map_added": added}

    def dataset_id(self) -> int:
        with source_session.SourceSessionLocal() as db:
            row = db.query(IngestDataset.id).filter_by(
                source_code=SOURCE_CODE, dataset_code=DATASET_CODE).one_or_none()
            return int(row[0]) if row else 0

    def ensure_dataset(self) -> int:
        """批次登记前确保数据集已登记；返回 dataset_id。"""
        existing = self.dataset_id()
        if existing:
            return existing
        return self.register_all()["dataset_id"]

    def register_batch(self, batch: Mapping[str, Any]) -> int:
        """写入一条批次血缘，返回 batch id；batch_no 重复时返回既有 id（幂等）。"""
        with source_session.SourceSessionLocal() as db:
            batch_no = str(batch.get("batch_no") or uuid.uuid4().hex)
            obj = db.query(IngestBatch).filter_by(batch_no=batch_no).one_or_none()
            if obj is None:
                obj = IngestBatch(batch_no=batch_no, dataset_id=int(batch.get("dataset_id") or 0))
                db.add(obj)
            allowed = ("source_code", "db_name", "table_name", "load_mode", "window_start",
                       "window_end", "source_file", "file_sha256", "rows_read", "rows_loaded",
                       "rows_rejected", "status", "error", "triggered_by", "started_at",
                       "finished_at", "duration_ms")
            for key in allowed:
                if key in batch and hasattr(obj, key):
                    value = batch[key]
                    if key == "error":
                        value = sanitize_text(value, 500)
                    setattr(obj, key, value)
            db.flush()
            batch_id = int(obj.id)
            db.commit()
        return batch_id

    def update_batch(self, batch_id: int, patch: Mapping[str, Any]) -> None:
        """更新批次状态/计数；error 强制脱敏。"""
        with source_session.SourceSessionLocal() as db:
            obj = db.query(IngestBatch).filter_by(id=batch_id).one_or_none()
            if obj is None:
                return
            allowed = ("rows_read", "rows_loaded", "rows_rejected", "file_sha256", "status",
                       "error", "finished_at", "duration_ms")
            for key in allowed:
                if key in patch and hasattr(obj, key):
                    value = patch[key]
                    if key == "error":
                        value = sanitize_text(value, 500)
                    setattr(obj, key, value)
            db.commit()

    def register_errors(self, batch_id: int, errors: Sequence[Mapping[str, Any]]) -> int:
        """坏行样本限量写入；raw_row 只保留源单元格文本，经脱敏截断。"""
        samples = list(errors)[:MAX_ROW_ERRORS]
        if not samples:
            return 0
        with source_session.SourceSessionLocal() as db:
            for err in samples:
                db.add(IngestRowError(
                    batch_id=int(batch_id),
                    row_no=int(err.get("row_no") or 0),
                    raw_row=sanitize_text(err.get("raw_row", ""), 1000),
                    reason=sanitize_text(err.get("reason", "bad_type"), 200),
                ))
            db.commit()
        return len(samples)

    def find_success_batch(self, window: tuple[date, date], day: date | None = None) -> int:
        """查同窗口当天已成功的批次（供 collector/OdsWriter 复用）。"""
        dataset_id = self.dataset_id()
        if not dataset_id:
            return 0
        start, end = window
        today = day or datetime.now().date()
        with source_session.SourceSessionLocal() as db:
            row = db.query(IngestBatch.id).filter(
                IngestBatch.dataset_id == dataset_id,
                IngestBatch.status == "success",
                IngestBatch.window_start == start,
                IngestBatch.window_end == end,
                IngestBatch.started_at >= datetime.combine(today, datetime.min.time()),
            ).order_by(IngestBatch.id.desc()).first()
            return int(row[0]) if row else 0


def validate_field_map_csv() -> list[str]:
    """校验 CSV 模板与 FIELD_SPECS 一致；返回差异列表（空表示一致）。"""
    if not FIELD_MAP_CSV.exists():
        return [f"缺少模板 {FIELD_MAP_CSV.name}"]
    with FIELD_MAP_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    expected = [(s, c, d, t, "true" if req else "false", cm)
                for s, c, d, t, req, cm in FIELD_SPECS]
    actual = [(r.get("src_field", ""), r.get("col_name", ""), r.get("data_type", ""),
               r.get("transform", ""), str(r.get("required", "")).strip().lower(),
               r.get("comment", "")) for r in rows]
    return [] if actual == expected else [f"field_map 模板与 FIELD_SPECS 不一致：{len(actual)} 行"]


def _main(argv: Sequence[str]) -> int:
    command = argv[0] if argv else "info"
    registrar = IngestRegistrar()
    if command == "init":
        problems = validate_field_map_csv()
        if problems:
            for p in problems:
                print(p)
            return 1
        print(registrar.register_all())
        return 0
    if command == "info":
        print({"source_code": SOURCE_CODE, "dataset_code": DATASET_CODE,
               "table": f"{DB_NAME}.{TABLE_NAME}", "primary_key": PRIMARY_KEY,
               "field_map_rows": len(FIELD_SPECS), "dataset_id": registrar.dataset_id()})
        return 0
    print(f"用法：python -m importer.ingest_registry {init|info}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
