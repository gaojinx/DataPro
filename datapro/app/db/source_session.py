"""数据源库（datapro_source）连接与迁移。

与业务库（app/db/session.py）完全独立：
- 独立 engine / SessionLocal，互不抢占连接池；
- 独立 Alembic 版本链（alembic_source/），业务库升级不会碰这张表；
- 启动顺序上必须先建数据源库，业务库的 0012 迁移依赖它做数据搬迁。

单独执行迁移：
    python -m app.db.source_session
"""
import logging
import re
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]      # datapro/

source_engine = create_engine(
    settings.source_db_url,
    pool_pre_ping=True,
    pool_size=3,
    pool_recycle=1800,
    connect_args={"connect_timeout": 3},   # 数据源库不可用时快速失败
)
SourceSessionLocal = sessionmaker(bind=source_engine, autoflush=False, expire_on_commit=False)

_SAFE_DB_NAME = re.compile(r"^[A-Za-z0-9_$]+$")


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic_source.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic_source"))
    return cfg


def ensure_database() -> None:
    """按需创建数据源库（默认关闭）。

    创建库需要账号具备全局 CREATE 权限；生产通常由 DBA/initContainer 预建，
    因此默认不自动创建，避免应用账号被授予过高权限。
    """
    if not settings.source_db_auto_create:
        return
    url = make_url(settings.source_db_url)
    db_name = url.database or ""
    if not db_name:
        raise ValueError("SOURCE_DATABASE_URL 未指定数据库名")
    if not _SAFE_DB_NAME.match(db_name):
        raise ValueError(f"数据库名不合法：{db_name!r}")
    try:
        server_engine = create_engine(url.set(database=None), poolclass=None,
                                      connect_args={"connect_timeout": 3})
        try:
            with server_engine.connect() as conn:
                conn.execute(text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "DEFAULT CHARACTER SET utf8mb4"))
                conn.commit()
            logger.info("数据源库已就绪：%s", db_name)
        finally:
            server_engine.dispose()
    except Exception as exc:  # noqa: BLE001 —— 权限不足是常见情况，交给下方迁移报明确错误
        logger.warning("自动创建数据源库失败（%s）：%s", db_name, exc)


def _hint(db_name: str) -> str:
    user = make_url(settings.source_db_url).username or "<user>"
    return (f"数据源库 `{db_name}` 不存在或当前账号 `{user}` 无权限访问。"
            "请先由管理员执行：\n"
            f"  CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4;\n"
            f"  GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{user}'@'%';\n"
            "或在具备 CREATE 权限的环境打开 SOURCE_DB_AUTO_CREATE=true。")


def init_source_db() -> None:
    """建表/迁移数据源库；老库（手工建表无版本号）自动 stamp 到 head。"""
    from alembic import command

    url = make_url(settings.source_db_url)
    db_name = url.database or ""
    ensure_database()

    try:
        insp = inspect(source_engine)
        tables = set(insp.get_table_names())
    except Exception as exc:  # noqa: BLE001 —— 统一转成可执行的运维提示
        raise RuntimeError(_hint(db_name)) from exc

    cfg = _alembic_config()
    if "ingest_source" in tables and "alembic_version" not in tables:
        # 手工建过表但没有版本记录：结构已等于基线，只打版本号不重放建表
        command.stamp(cfg, "head")
        logger.info("数据源库已存在手工表结构，标记版本到 head")
    else:
        command.upgrade(cfg, "head")
    logger.info("数据源库迁移完成：%s", db_name)


def current_version() -> str | None:
    """当前数据源库的迁移版本，供健康检查/脚本使用。"""
    with source_engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_source_db()
    print(f"source db = {settings.source_db_url.rsplit('/', 1)[-1]}, version = {current_version()}")
