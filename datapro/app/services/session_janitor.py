"""会话资源回收（S07）。

定期清理长时间无活动的会话：
  1. DROP 该会话的临时表 dp_uploads.f_*
  2. DROP USER 会话只读账号
  3. 文件记录保留（用户仍可下载），但标记临时表已过期
  4. 顺带清理孤儿表（不属于任何存活会话记录的 f_* 表）

管理员也可通过 POST /admin/sessions/sweep 手动触发。

多实例加固：sweep 全程持跨进程文件锁，避免 worker 定时与 admin 手动并发；
过期列表在 DROP 前二次校验活跃时间，避免误删正在使用的会话。
"""
import logging
from pathlib import Path

import sqlalchemy as sa

from app.config import settings
from app.services.db_identity import TEMP_DB, session_db_user
from app.services.file_store import file_store
from app.services.session_registry import session_registry
from app.services.shared_json import file_lock

logger = logging.getLogger("datapro.janitor")
STALE_MSG = "临时表已过期清理，重新上传后可再次查询"
_SWEEP_LOCK = Path(__file__).resolve().parents[2] / "data" / "session_sweep.lock"


def sweep(ttl_hours: int | None = None) -> dict:
    ttl = settings.session_ttl_hours if ttl_hours is None else ttl_hours
    stats = {"ttl_hours": ttl, "sessions": 0, "tables": 0, "users": 0, "orphan_tables": 0}

    # 跨进程互斥：worker 定时与 admin 手动触发不能并发执行回收
    with file_lock(_SWEEP_LOCK):
        expired = session_registry.expired(ttl)
        engine = sa.create_engine(settings.upload_db_url, pool_pre_ping=True)
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                for sid in expired:
                    # 二次校验：api 进程可能在 expired() 之后 touch 过该会话
                    if session_registry.recheck_active(sid, ttl):
                        logger.info("会话 %s 已重新活跃，跳过回收", sid)
                        continue
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
        finally:
            engine.dispose()

    if stats["sessions"] or stats["orphan_tables"]:
        logger.info("会话资源回收完成 %s", stats)
    return stats
