"""数据库连接与迁移（Alembic）。"""
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings

BASE_DIR = Path(__file__).resolve().parents[2]      # datapro/

engine = create_engine(
    settings.app_database_url,
    pool_pre_ping=True,
    pool_size=5,
    pool_recycle=1800,
    connect_args={"connect_timeout": 3},   # 业务库不可用时快速失败，不拖慢主链路
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表/迁移：先数据源库，再业务库；老库自动 stamp 到 head。

    顺序不可颠倒：业务库 0012 迁移要把 ingest_* 表的数据复制到数据源库，
    目标表必须已存在。
    """
    from alembic import command
    from alembic.config import Config

    from app.db.source_session import init_source_db

    init_source_db()

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    insp = inspect(engine)
    if insp.has_table("query_record") and not insp.has_table("alembic_version"):
        # 历史库由 create_all 建成，表结构已等于基线：打版本号，不重放建表
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
