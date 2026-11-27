"""数据导入承接层（ODS）：来源、数据集、批次、字段映射、坏行。

背景：后续会由外部系统抓取大量数据导入，一个数据集对应一张物理表。
本层只负责「登记 + 血缘 + 幂等 + 可追溯」，不存放业务数据本身。

设计要点：
- 物理表仍然是一数据集一张真实表（可用 `ods_{source}_{dataset}`），不采用单表 JSON 堆叠，
  否则 Text2SQL 只能生成 JSON_EXTRACT，模型易错、校验器白名单也失去意义。
- `meta_table` 唯一键从 (table_name) 收紧为 (db_name, table_name)，
  `meta_column` 补 db_name 并改为 (db_name, table_name, col_name)，
  否则多库同名表（如 ods_a.orders 与 dwd_shop.orders）无法共存。

Revision ID: 0011_ingest_layer
Revises: 0010_token_version
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_ingest_layer"
down_revision = "0010_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- 来源系统 ----------
    op.create_table(
        "ingest_source",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(32), nullable=False),          # 短码，参与物理表命名
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="scraper"),
        sa.Column("owner", sa.String(64), nullable=False, server_default=""),
        sa.Column("contact", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code", name="uq_ingest_source_code"),
    )

    # ---------- 数据集：一个数据集 = 一张目标物理表 ----------
    op.create_table(
        "ingest_dataset",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_code", sa.String(32), nullable=False),
        sa.Column("dataset_code", sa.String(64), nullable=False),
        sa.Column("db_name", sa.String(64), nullable=False, server_default="dwd_shop"),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("cn_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("grain", sa.String(128), nullable=True),         # 粒度：一行代表什么
        sa.Column("primary_key", sa.String(255), nullable=True),   # 自然主键（逗号分隔），upsert 去重依据
        sa.Column("incremental_col", sa.String(64), nullable=True),  # 增量/水位字段
        sa.Column("partition_col", sa.String(64), nullable=True),  # 分区列，默认 dt
        sa.Column("load_mode", sa.String(16), nullable=False, server_default="incremental"),
        sa.Column("dtype_policy", sa.String(16), nullable=False, server_default="typed"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("meta_registered", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_code", "dataset_code", name="uq_ingest_dataset_code"),
        sa.UniqueConstraint("db_name", "table_name", name="uq_ingest_dataset_table"),
    )
    op.create_index("ix_ingest_dataset_table", "ingest_dataset", ["table_name"])

    # ---------- 字段映射：源字段 → 目标列，含类型与清洗规则 ----------
    op.create_table(
        "ingest_field_map",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("src_field", sa.String(128), nullable=False),
        sa.Column("col_name", sa.String(128), nullable=False),
        sa.Column("data_type", sa.String(32), nullable=False, server_default="varchar"),
        sa.Column("transform", sa.String(64), nullable=True),      # trim/date/int/decimal/enum_map
        sa.Column("enum_map", sa.Text(), nullable=True),            # JSON：源值 → 标准值
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("comment", sa.String(255), nullable=True),
        sa.UniqueConstraint("dataset_id", "schema_version", "col_name",
                            name="uq_ingest_field_map"),
    )
    op.create_index("ix_ingest_field_map_dataset", "ingest_field_map", ["dataset_id"])

    # ---------- 批次：一次导入执行，血缘与幂等锚点 ----------
    op.create_table(
        "ingest_batch",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_no", sa.String(32), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        # 冗余快照：数据集改名/删除后，历史批次仍能自解释
        sa.Column("source_code", sa.String(32), nullable=False, server_default=""),
        sa.Column("db_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("load_mode", sa.String(16), nullable=False, server_default="incremental"),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("source_file", sa.String(255), nullable=True),
        sa.Column("file_sha256", sa.String(64), nullable=True),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_loaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint("batch_no", name="uq_ingest_batch_no"),
    )
    op.create_index("ix_ingest_batch_dataset", "ingest_batch", ["dataset_id", "started_at"])
    op.create_index("ix_ingest_batch_status", "ingest_batch", ["status", "started_at"])
    op.create_index("ix_ingest_batch_file", "ingest_batch", ["file_sha256"])

    # ---------- 坏行样本：限量保留，便于修正映射后重放 ----------
    op.create_table(
        "ingest_row_error",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_row", sa.Text(), nullable=True),            # 截断样本，非全量
        sa.Column("reason", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ingest_row_error_batch", "ingest_row_error", ["batch_id"])

    # ---------- 元数据表支持多库 ----------
    op.add_column("meta_column", sa.Column("db_name", sa.String(64), nullable=False,
                                           server_default="dwd_shop"))
    op.drop_index("ix_meta_column_table_name", table_name="meta_column")
    op.drop_constraint("uq_meta_column", "meta_column", type_="unique")
    op.create_unique_constraint("uq_meta_column", "meta_column",
                                ["db_name", "table_name", "col_name"])
    op.create_index("ix_meta_column_table_name", "meta_column", ["db_name", "table_name"])

    op.drop_constraint("uq_meta_table_name", "meta_table", type_="unique")
    op.create_unique_constraint("uq_meta_table_db", "meta_table", ["db_name", "table_name"])


def downgrade() -> None:
    op.drop_constraint("uq_meta_table_db", "meta_table", type_="unique")
    op.create_unique_constraint("uq_meta_table_name", "meta_table", ["table_name"])

    op.drop_index("ix_meta_column_table_name", table_name="meta_column")
    op.drop_constraint("uq_meta_column", "meta_column", type_="unique")
    op.create_unique_constraint("uq_meta_column", "meta_column", ["table_name", "col_name"])
    op.create_index("ix_meta_column_table_name", "meta_column", ["table_name"])
    op.drop_column("meta_column", "db_name")

    op.drop_index("ix_ingest_row_error_batch", table_name="ingest_row_error")
    op.drop_table("ingest_row_error")
    op.drop_index("ix_ingest_batch_file", table_name="ingest_batch")
    op.drop_index("ix_ingest_batch_status", table_name="ingest_batch")
    op.drop_index("ix_ingest_batch_dataset", table_name="ingest_batch")
    op.drop_table("ingest_batch")
    op.drop_index("ix_ingest_field_map_dataset", table_name="ingest_field_map")
    op.drop_table("ingest_field_map")
    op.drop_index("ix_ingest_dataset_table", table_name="ingest_dataset")
    op.drop_table("ingest_dataset")
    op.drop_table("ingest_source")
