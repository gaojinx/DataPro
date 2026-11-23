"""会话资源回收（S07）。

定期清理长时间无活动的会话：
  1. DROP 该会话的临时表 dp_uploads.f_*
  2. DROP USER 会话只读账号
  3. 文件记录保留（用户仍可下载），但标记临时表已过期
  4. 顺带清理孤儿表（不属于任何存活会话记录的 f_* 表）

管理员也可通过 POST /admin/sessions/sweep 手动触发。
"""
import logging

import sqlalchemy as sa

from app.config import settings
from app.services.db_identity import TEMP_DB, session_db_user
from app.services.file_store import file_store
from app.services.session_registry import session_registry

logger = logging.getLogger("datapro.janitor")
STALE_MSG = "临时表已过期清理，重新上传后可再次查询"


def sweep(ttl_hours: int | None = None) -> dict:
    ttl = settings.session_ttl_hours if ttl_hours is None else ttl_hours
    expired = session_registry.expired(ttl)
    stats = {"ttl_hours": ttl, "sessions": 0, "tables": 0, "users": 0, "orphan_tables": 0}

    eng = sa.create_engine(settings.upload_db_url, pool_pre_ping=True)
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for sid in expired:
            tables = []
            for f in file_store.list(sid):
                parsed = f.get("parsed")
                if parsed:
                    tables.extend(t["table"] for t in (parsed.get("tables") or [parsed]))
                    file_store.update(f["file_id"], {"parsed": None, "parse_error": STALE_MSG})
            for t in tables:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS `{TEMP_DB}`.`{t}`"))
            conn.execute(sa.text(f"DROP USER IF EXISTS '{session_db_user(sid)}'@'%'"))
            session_registry.remove(sid)
            stats["sessions"] += 1
            stats["tables"] += len(tables)
            stats["users"] += 1

        # 孤儿表：库中存在但不属于任何存活会话记录的 f_* 表
        alive = set()
        for sid in session_registry.all():
            for f in file_store.list(sid):
                parsed = f.get("parsed")
                if parsed:
                    alive.update(t["table"] for t in (parsed.get("tables") or [parsed]))
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": TEMP_DB}).fetchall()
        for (name,) in rows:
            if name.startswith("f_") and name not in alive:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS `{TEMP_DB}`.`{name}`"))
                stats["orphan_tables"] += 1
    eng.dispose()

    if stats["sessions"] or stats["orphan_tables"]:
        logger.info("会话资源回收完成 %s", stats)
    return stats
