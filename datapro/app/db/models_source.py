"""数据源库模型（独立库 datapro_source）：只放「数据源登记 + 血缘」。

拆分原因：
- 导入承接层是写入密集、可独立备份/清理的运维数据，与 query_record/audit_log 这类
  业务审计数据的生命周期和授权边界不同；
- 单独库可给摄取服务单独账号，避免它拥有业务库写权限。

边界：这里只放登记与血缘，**不放业务数据本身**。一个数据集对应一张真实物理表。
"""
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Integer, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BaseSource(DeclarativeBase):
    """数据源库的声明基类，与 app.db.models.Base 完全独立。"""


class IngestSource(BaseSource):
    """来源系统：一个抓取/接口/上传通道。code 参与物理表命名（ods_{code}_{dataset}）。"""
    __tablename__ = "ingest_source"
    __table_args__ = (UniqueConstraint("code", name="uq_ingest_source_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="scraper")  # scraper/api/upload/manual
    owner: Mapped[str] = mapped_column(String(64), default="")
    contact: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class IngestDataset(BaseSource):
    """数据集：一个数据集对应一张真实物理表，登记落库位置、主键、增量策略与版本。"""
    __tablename__ = "ingest_dataset"
    __table_args__ = (
        UniqueConstraint("source_code", "dataset_code", name="uq_ingest_dataset_code"),
        UniqueConstraint("db_name", "table_name", name="uq_ingest_dataset_table"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_code: Mapped[str] = mapped_column(String(32))
    dataset_code: Mapped[str] = mapped_column(String(64))
    db_name: Mapped[str] = mapped_column(String(64), default="dwd_shop")
    table_name: Mapped[str] = mapped_column(String(128), index=True)
    cn_name: Mapped[str] = mapped_column(String(128), default="")
    grain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    primary_key: Mapped[str | None] = mapped_column(String(255), nullable=True)      # 逗号分隔
    incremental_col: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partition_col: Mapped[str | None] = mapped_column(String(64), nullable=True)
    load_mode: Mapped[str] = mapped_column(String(16), default="incremental")        # full/incremental/append
    dtype_policy: Mapped[str] = mapped_column(String(16), default="typed")           # typed/all_varchar
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft")                 # draft/active/paused/deprecated
    meta_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class IngestFieldMap(BaseSource):
    """字段映射：源字段 → 目标列，含类型、清洗规则与枚举归一化表。"""
    __tablename__ = "ingest_field_map"
    __table_args__ = (UniqueConstraint("dataset_id", "schema_version", "col_name",
                                       name="uq_ingest_field_map"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(Integer, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    src_field: Mapped[str] = mapped_column(String(128))
    col_name: Mapped[str] = mapped_column(String(128))
    data_type: Mapped[str] = mapped_column(String(32), default="varchar")
    transform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enum_map: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)


class IngestBatch(BaseSource):
    """导入批次：一次执行的完整血缘（文件、校验和、行数、状态），幂等与回滚的锚点。"""
    __tablename__ = "ingest_batch"
    __table_args__ = (UniqueConstraint("batch_no", name="uq_ingest_batch_no"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(32))
    dataset_id: Mapped[int] = mapped_column(Integer, index=True)
    source_code: Mapped[str] = mapped_column(String(32), default="")
    db_name: Mapped[str] = mapped_column(String(64), default="")
    table_name: Mapped[str] = mapped_column(String(128), default="")
    load_mode: Mapped[str] = mapped_column(String(16), default="incremental")
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    rows_loaded: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class IngestRowError(BaseSource):
    """坏行样本：限量保留，便于修正映射后定向重放，不存全量原始数据。"""
    __tablename__ = "ingest_row_error"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(BigInteger, index=True)
    row_no: Mapped[int] = mapped_column(Integer, default=0)
    raw_row: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
