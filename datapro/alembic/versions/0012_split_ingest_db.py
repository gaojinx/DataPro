"""把数据源相关表从业务库迁到独立库 datapro_source。

迁移策略（只搬 5 张导入承接表，不动 meta_table/meta_column 等查询热路径表）：
1. 若 SOURCE_DATABASE_URL 与业务库同名 → 视为单库部署，直接跳过（保持原状）；
2. 目标库必须先由数据源迁移树建好表（init_db 会保证顺序），否则中止；
3. 逐表复制数据 → 校验目标行数不少于源行数 → 才 DROP 源表。
   任一步失败都在 DROP 之前抛出，最坏情况是数据重复但两边都在，不会丢血缘。

Revision ID: 0012_split_ingest_db
Revises: 0011_ingest_layer
"""
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import create_engine, inspect, pool
from sqlalchemy.engine import make_url

revision = "0012_split_ingest_db"
down_revision = "0011_ingest_layer"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# 顺序无外键依赖，按「先主数据后明细」便于人工核对
TABLES = ("ingest_source", "ingest_dataset", "ingest_field_map",
          "ingest_batch", "ingest_row_error")
CHUNK = 500


def _source_url() -> str:
    from app.config import settings
    return settings.source_db_url


def _business_db_name() -> str:
    from app.config import settings
    return make_url(settings.app_database_url).database or ""


def _same_database(source_url: str) -> bool:
    """库名相同即视为同一库：拆分前提就是库名不同，此时宁可放弃也不误删。"""
    return (make_url(source_url).database or "") == _business_db_name()


def _copy_table(bind, src_conn, table_name: str) -> tuple[int, int]:
    """从业务库复制一张表到数据源库，返回 (源行数, 目标行数)。"""
    from app.db.models_source import BaseSource

    table = BaseSource.metadata.tables[table_name]
    rows = [dict(r._mapping) for r in bind.execute(sa.text(f"SELECT * FROM `{table_name}`"))]
    if rows:
        stmt = sa.dialects.mysql.insert(table).prefix_with("IGNORE")
        for i in range(0, len(rows), CHUNK):
            src_conn.execute(stmt, rows[i:i + CHUNK])
        src_conn.commit()
    target = src_conn.execute(sa.text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar_one()
    return len(rows), int(target)


def upgrade() -> None:
    source_url = _source_url()
    if _same_database(source_url):
        logger.warning("数据源库与业务库同名（%s），跳过拆分：单库部署模式", _business_db_name())
        return

    bind = op.get_bind()
    business_tables = set(inspect(bind).get_table_names())
    present = [t for t in TABLES if t in business_tables]
    if not present:
        logger.info("业务库已无数据源表，跳过拆分")
        return

    source_engine = create_engine(source_url, poolclass=pool.NullPool)
    try:
        with source_engine.connect() as src_conn:
            missing = [t for t in present
                       if not inspect(src_conn).has_table(t)]
            if missing:
                raise RuntimeError(
                    f"数据源库缺少表 {missing}。请先执行数据源迁移：\n"
                    "  python -m app.db.source_session\n"
                    "（init_db() 会自动按 数据源库 → 业务库 的顺序执行）")

            copied: dict[str, tuple[int, int]] = {}
            for table_name in present:
                src_rows, dst_rows = _copy_table(bind, src_conn, table_name)
                if dst_rows < src_rows:
                    raise RuntimeError(
                        f"{table_name} 复制不完整（源 {src_rows} 行 / 目标 {dst_rows} 行），"
                        "已中止，业务库原表未删除")
                copied[table_name] = (src_rows, dst_rows)

        # 只有全部表校验通过后才删源表：宁可重复，不可丢失
        for table_name in present:
            op.drop_table(table_name)
        logger.info("数据源表已迁出业务库：%s", ", ".join(f"{k}({v[0]}→{v[1]})"
                                                     for k, v in copied.items()))
    finally:
        source_engine.dispose()


def downgrade() -> None:
    """回滚：在业务库重建表并把数据复制回来。

    数据源库的表保留不删（非破坏性回滚），仅在确认回滚成功后由运维手工清理。
    """
    source_url = _source_url()
    if _same_database(source_url):
        return

    from app.db.models_source import BaseSource

    bind = op.get_bind()
    source_engine = create_engine(source_url, poolclass=pool.NullPool)
    try:
        with source_engine.connect() as src_conn:
            for table_name in TABLES:
                BaseSource.metadata.tables[table_name].create(bind=bind, checkfirst=True)
                if not inspect(src_conn).has_table(table_name):
                    continue
                rows = [dict(r._mapping) for r in
                        src_conn.execute(sa.text(f"SELECT * FROM `{table_name}`"))]
                if not rows:
                    continue
                stmt = sa.dialects.mysql.insert(
                    BaseSource.metadata.tables[table_name]).prefix_with("IGNORE")
                for i in range(0, len(rows), CHUNK):
                    bind.execute(stmt, rows[i:i + CHUNK])
            logger.info("数据源表已回滚到业务库：%s", ", ".join(TABLES))
    finally:
        source_engine.dispose()
