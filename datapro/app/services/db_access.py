"""用户级数据表权限（A02 / 权限与脱敏设计 S1）。

规则：
- 管理员不受限（由调用方判断 role 后跳过）；
- 普通用户**未配置**（无记录）→ 返回 None，沿用全局表白名单，保持既有行为；
- 普通用户**已配置**→ 只允许访问所列数据表，越权 SQL 在校验阶段直接 E003 拦截；
  显式配置为空集合表示该用户不允许访问任何业务表（谨慎使用）。

存储：业务库 `db_access` 表优先；库不可用时降级 `data/db_access.json`。
读取结果缓存 `CACHE_TTL` 秒，写入后立即失效，权限变更无需重启。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("datapro.db_access")

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "db_access.json"
DEFAULT_DB = "dwd_shop"
CACHE_TTL = 60.0


def active_tables(rows: list[tuple[str, datetime | None]],
                  now: datetime | None = None) -> set[str]:
    """过滤过期授权：`expires_at` 为空表示永久，否则必须晚于当前时间。

    注意「已配置但全部过期」与「从未配置」不同：调用方用 rows 非空判定已配置，
    避免过期后静默回退到全局白名单（那是提权，不是放宽）。
    """
    now = now or datetime.now()
    return {str(name).lower() for name, expires_at in rows
            if expires_at is None or expires_at > now}


class DbAccessRegistry:
    def __init__(self, path: Path | None = None, use_db: bool = True):
        self._path = path or DATA_FILE
        self._use_db = use_db
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, frozenset[str] | None]] = {}

    # ---- JSON 兜底 ----

    def _read_json(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("用户表权限文件不可读：%s", exc)
            return {}

    def _write_json(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _from_json(self, username: str) -> set[str] | None:
        rec = self._read_json().get(username)
        if not rec:
            return None
        tables = rec.get("tables")
        return {str(t).lower() for t in tables} if isinstance(tables, list) else None

    # ---- 业务库 ----

    def _from_db(self, username: str) -> set[str] | None:
        from app.db.models import DbAccess
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            rows = (db.query(DbAccess.table_name, DbAccess.expires_at)
                    .filter(DbAccess.username == username).all())
        if not rows:
            return None
        return active_tables([(r[0], r[1]) for r in rows])

    def _write_db(self, username: str, tables: list[str], granted_by: str,
                  user_id: int | None, db_names: dict[str, str]) -> None:
        from app.db.models import DbAccess
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            if user_id is None:
                # user_id 之前恒为 NULL，等于白存；这里按账号回填，便于按 id 关联与级联清理
                from app.db.models import User
                row = db.query(User.id).filter(User.username == username).one_or_none()
                user_id = int(row[0]) if row else None
            db.query(DbAccess).filter(DbAccess.username == username).delete()
            for name in tables:
                db.add(DbAccess(user_id=user_id, username=username,
                                db_name=db_names.get(name, DEFAULT_DB), table_name=name,
                                granted_by=granted_by, granted_at=datetime.now(),
                                expires_at=None))     # 手工整体设置 = 永久授权
            db.commit()

    def _clear_db(self, username: str) -> None:
        from app.db.models import DbAccess
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            db.query(DbAccess).filter(DbAccess.username == username).delete()
            db.commit()

    def _db_names(self) -> dict[str, str]:
        """表名 → 库名映射（来自元数据），取不到时用默认库。"""
        try:
            from app.services.metadata_store import metadata_store
            return {str(t["table_name"]).lower(): t.get("db_name") or DEFAULT_DB
                    for t in metadata_store.tables()}
        except Exception:  # noqa: BLE001 —— 元数据不可用时用默认库名
            return {}

    # ---- 对外接口 ----

    def tables_for(self, username: str, refresh: bool = False) -> set[str] | None:
        """返回用户可访问表集合；None 表示未配置（沿用全局白名单）。"""
        username = (username or "").strip()
        if not username:
            return None
        now = time.monotonic()
        if not refresh:
            hit = self._cache.get(username)
            if hit and now - hit[0] < CACHE_TTL:
                return None if hit[1] is None else set(hit[1])

        value: set[str] | None = None
        db_ok = False
        if self._use_db:
            try:
                value = self._from_db(username)
                db_ok = True
            except Exception as exc:  # noqa: BLE001 —— 业务库不可用降级 JSON
                logger.warning("读取 db_access 表失败，降级 JSON：%s", exc)
        if not db_ok:
            value = self._from_json(username)

        self._cache[username] = (now, None if value is None else frozenset(value))
        return value

    def configured(self, username: str) -> bool:
        return self.tables_for(username) is not None

    def set_tables(self, username: str, tables: list[str] | None,
                   granted_by: str = "", user_id: int | None = None) -> list[str] | None:
        """保存用户表权限；tables=None 表示清除配置（回退继承全局白名单）。"""
        username = (username or "").strip()
        if not username:
            raise ValueError("username 必填")

        if tables is None:
            self._clear(username)
            return None

        clean = sorted({str(t).strip().lower() for t in tables if str(t).strip()})
        if self._use_db:
            try:
                self._write_db(username, clean, granted_by, user_id, self._db_names())
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级 JSON
                logger.warning("写入 db_access 表失败，降级 JSON：%s", exc)
                self._write_json_entry(username, clean, granted_by)
        else:
            self._write_json_entry(username, clean, granted_by)

        with self._lock:
            self._cache.pop(username, None)
        return clean

    def grant_table(self, username: str, table_name: str, granted_by: str = "",
                    expires_at: datetime | None = None,
                    db_name: str | None = None) -> set[str]:
        """增量授予单表（审批用）：不动该用户其他表的授权与有效期。

        与 `set_tables`（整体覆盖 = 永久）区分：审批授予必须能带有效期，
        覆盖式写入会把其他行的 expires_at 一起抹成永久，等于越权放大。
        """
        username = (username or "").strip()
        clean = (table_name or "").strip().lower()
        if not username or not clean:
            raise ValueError("username 与 table_name 必填")

        if self._use_db:
            from app.db.models import DbAccess, User
            from app.db.session import SessionLocal

            with SessionLocal() as db:
                user = db.query(User.id).filter(User.username == username).one_or_none()
                if user is None:
                    raise ValueError(f"用户不存在：{username}")
                target_db = db_name or self._db_names().get(clean, DEFAULT_DB)
                row = (db.query(DbAccess)
                       .filter(DbAccess.username == username,
                               DbAccess.db_name == target_db,
                               DbAccess.table_name == clean).one_or_none())
                if row is None:
                    row = DbAccess(username=username, db_name=target_db, table_name=clean)
                row.user_id = int(user[0])
                row.granted_by = granted_by
                row.granted_at = datetime.now()
                row.expires_at = expires_at
                db.add(row)
                db.commit()
        else:
            # 文件兜底没有有效期语义，仅供单机/测试；生产审批走库
            with self._lock:
                data = self._read_json()
                tables = set((data.get(username) or {}).get("tables") or [])
                tables.add(clean)
                data[username] = {"tables": sorted(tables), "by": granted_by,
                                  "at": datetime.now().isoformat(timespec="seconds")}
                self._write_json(data)

        with self._lock:
            self._cache.pop(username, None)
        return set(self.tables_for(username, refresh=True) or set())

    def _write_json_entry(self, username: str, tables: list[str], granted_by: str) -> None:
        with self._lock:
            data = self._read_json()
            data[username] = {
                "tables": tables, "by": granted_by,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            self._write_json(data)

    def _clear(self, username: str) -> None:
        if self._use_db:
            try:
                self._clear_db(username)
            except Exception as exc:  # noqa: BLE001 —— 清库失败仍清 JSON 与缓存
                logger.warning("清除 db_access 表失败：%s", exc)
        with self._lock:
            data = self._read_json()
            if username in data:
                data.pop(username)
                self._write_json(data)
            self._cache.pop(username, None)


db_access = DbAccessRegistry()
