"""ODS 写入：dp_ods_writer 专属 engine + 唯一键 upsert + 同窗口成功批次去重。

红线：本模块只写 dwd_shop.ods_xianzhi_info_analysis 一张表；
连接串从环境变量 ODS_WRITER_URL 读取，源码/配置模板不出现真实口令，
也绝不把 writer engine 用于任何查询链路。
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.db import source_session
from app.db.models_source import IngestBatch
from worker.xianzhi_fields import DATA_COLUMNS

logger = logging.getLogger("datapro.ingest.ods")

ODS_URL_ENV = "ODS_WRITER_URL"


class OdsWriterError(RuntimeError):
    """ODS 写入失败；消息只含类型与原因码，不含连接串/口令。"""


class OdsWriter:
    """一次采集一张表的 writer；engine 延迟创建，便于测试注入。"""

    TABLE = "dwd_shop.ods_xianzhi_info_analysis"
    UPDATE_COLUMNS: tuple[str, ...] = tuple(
        c for c in DATA_COLUMNS if c != "dt") + ("ingest_batch_id", "source_url")
    PARAM_COLUMNS: tuple[str, ...] = DATA_COLUMNS + ("ingest_batch_id", "source_url")

    def __init__(self, url: str = "", engine: Engine | None = None, dataset_id: int = 0) -> None:
        self.url = url or os.environ.get(ODS_URL_ENV, "")
        self.dataset_id = dataset_id
        self.engine: Engine | None = engine
        if self.engine is None:
            if not self.url:
                raise OdsWriterError("ODS_WRITER_URL 未配置（禁止明文口令落代码）")
            # 只在这里创建受限账号连接；连接串异常不打印（可能含口令）
            try:
                self.engine = create_engine(self.url, pool_pre_ping=True, pool_size=2)
            except Exception as exc:  # noqa: BLE001
                raise OdsWriterError(f"ODS engine 初始化失败（{type(exc).__name__}）") from exc

    def _sql_upsert(self) -> str:
        cols = ", ".join(self.PARAM_COLUMNS)
        params = ", ".join(f":{c}" for c in self.PARAM_COLUMNS)
        dialect = self.engine.dialect.name if self.engine is not None else "mysql"
        if dialect == "sqlite":
            # 仅用于本地/单测：sqlite 无 ON DUPLICATE KEY UPDATE，UNIQUE 索引下 REPLACE 等价幂等。
            return f"INSERT OR REPLACE INTO {self.TABLE} ({cols}) VALUES ({params})"
        updates = ", ".join(f"{c} = new.{c}" for c in self.UPDATE_COLUMNS)
        return (f"INSERT INTO {self.TABLE} ({cols}) VALUES ({params}) AS new "
                f"ON DUPLICATE KEY UPDATE {updates}")

    def upsert(self, rows: Sequence[dict[str, Any]], batch_id: int = 0,
               source_url: str = "") -> int:
        """按 (dt, plan_id, media, landing_page_name) upsert，返回处理行数。"""
        if not rows:
            return 0
        dialect = self.engine.dialect.name if self.engine is not None else "mysql"
        payload: list[dict[str, Any]] = []
        for row in rows:
            item = {c: row.get(c) for c in DATA_COLUMNS}
            if dialect == "sqlite":
                # 仅测试路径：sqlite3 不接受 date/Decimal 绑定值
                for col, value in list(item.items()):
                    if isinstance(value, date):
                        item[col] = value.isoformat()
                    elif isinstance(value, Decimal):
                        item[col] = float(value)
            item["ingest_batch_id"] = batch_id or None
            item["source_url"] = source_url or ""
            payload.append(item)
        statement = text(self._sql_upsert())
        try:
            with self.engine.begin() as conn:  # type: ignore[union-attr]
                conn.execute(statement, payload)
        except Exception as exc:  # noqa: BLE001 —— 连接串/口令可能出现在 exc 中
            logger.warning("ODS upsert 失败：%s", type(exc).__name__)
            raise OdsWriterError(f"ODS upsert 失败（{type(exc).__name__}）") from exc
        return len(payload)

    def find_success_batch(self, window: tuple[date, date], day: date | None = None) -> int:
        """同窗口当天已有 success 批次 → 返回其 id（0 表示无），用于多实例重复触发兜底。"""
        if not self.dataset_id:
            return 0
        start, end = window
        today = day or datetime.now().date()
        try:
            with source_session.SourceSessionLocal() as db:
                query = db.query(IngestBatch.id).filter(
                    IngestBatch.dataset_id == self.dataset_id,
                    IngestBatch.status == "success",
                    IngestBatch.window_start == start,
                    IngestBatch.window_end == end,
                    IngestBatch.started_at >= datetime.combine(today, datetime.min.time()),
                )
                row = query.order_by(IngestBatch.id.desc()).first()
                return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001 —— 血缘库不可用时按「未找到」处理，不阻断采集
            logger.warning("成功批次查询失败：%s", type(exc).__name__)
            return 0

    def dispose(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


def iter_batches(rows: Sequence[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    """分批写出，避免一次性拼超大参数列表。"""
    step = max(1, int(size))
    for i in range(0, len(rows), step):
        yield list(rows[i:i + step])
