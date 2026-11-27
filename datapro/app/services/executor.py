"""只读执行引擎（HLD §3）：独立连接池、会话级数据库身份、超时、行数截断。"""
import asyncio
import logging
import re
import threading
from datetime import date

import sqlalchemy as sa

from app.config import settings
from app.core.errors import DataProError, E006
from app.core.time_window import TimeWindow, default_window
from app.services.db_identity import session_db_url
from app.services.session_registry import session_registry

logger = logging.getLogger("datapro.executor")

# 时间占位符：主用 {date_start}/{date_end}；{date}/{start_date}/{end_date} 为历史兼容别名，
# 一律绑定到已解析的时间窗，模型不再自行做日期算术（time_window.resolve）。
_PLACEHOLDER = re.compile(r"\{(date_start|date_end|date|start_date|end_date)\}")
MAX_SESSION_ENGINES = 50


def inject_dates(sql: str, window: TimeWindow | None = None) -> str:
    """把时间占位符替换为确定性解析出的具体日期。"""
    w = window or default_window()
    mapping = {
        "date_start": w.start.isoformat(),
        "date_end": w.end.isoformat(),
        "start_date": w.start.isoformat(),
        "end_date": w.end.isoformat(),
        "date": w.start.isoformat(),
    }
    return _PLACEHOLDER.sub(lambda m: mapping[m.group(1)], sql)


class ExecutorService:
    def __init__(self):
        self._engine: sa.Engine | None = None
        self._session_engines: dict[str, sa.Engine] = {}
        self._engine_lock = threading.Lock()
        self._sem = asyncio.Semaphore(settings.exec_max_concurrency)

    def _get_engine(self) -> sa.Engine:
        """无会话上下文时使用公共只读账号（仅 dwd_shop）。"""
        if self._engine is None:
            self._engine = sa.create_engine(
                settings.data_source_url,
                pool_size=10, max_overflow=10, pool_pre_ping=True,
            )
        return self._engine

    def _has_files(self, session_id: str) -> bool:
        """会话是否已有解析文件表（决定是否需要会话级账号）。"""
        from app.services.file_store import file_store
        try:
            return bool(file_store.parsed_files(session_id))
        except Exception:  # noqa: BLE001 —— 读取失败时按无文件处理
            return False

    def _engine_for(self, session_id: str) -> sa.Engine:
        """有文件会话用会话级账号（仅能读公共数仓 + 本会话文件表）；
        无文件会话沿用共享只读账号（共享账号已无 dp_uploads 权限，安全等价）。"""
        if not session_id or not self._has_files(session_id):
            return self._get_engine()
        with self._engine_lock:
            eng = self._session_engines.get(session_id)
            if eng is None:
                eng = sa.create_engine(
                    session_db_url(session_id),
                    pool_size=2, max_overflow=2, pool_pre_ping=True, pool_recycle=1800,
                )
                if len(self._session_engines) >= MAX_SESSION_ENGINES:
                    _, old = self._session_engines.popitem()
                    old.dispose()
                self._session_engines[session_id] = eng
            return eng

    async def execute(self, sql: str, user_id: str, session_id: str = "",
                      window: TimeWindow | None = None) -> dict:
        if session_id:
            session_registry.touch(session_id)  # S07：查询即视为活跃
        async with self._sem:
            return await asyncio.to_thread(self._execute_sync, sql, user_id, session_id, window)

    async def estimate_scan(self, sql: str, session_id: str = "",
                            window: TimeWindow | None = None) -> int | None:
        """A09：EXPLAIN 预估扫描行数；失败返回 None（不阻断主链路）。"""
        if not settings.scan_guard_enabled or settings.max_scan_rows <= 0:
            return None
        async with self._sem:
            return await asyncio.to_thread(self._estimate_sync, sql, session_id, window)

    def _estimate_sync(self, sql: str, session_id: str = "",
                       window: TimeWindow | None = None) -> int | None:
        final_sql = inject_dates(sql, window)
        try:
            with self._engine_for(session_id).connect() as conn:
                conn.execute(sa.text(f"SET SESSION max_execution_time = {settings.exec_timeout_ms}"))
                plan = conn.execute(sa.text("EXPLAIN " + final_sql)).mappings().all()
        except Exception as exc:  # noqa: BLE001 —— 预估失败按放行处理
            logger.warning("扫描量预估失败：%s | SQL=%s", exc, final_sql)
            return None
        total = 0
        for row in plan:
            try:
                total += int(row.get("rows") or 0)
            except (TypeError, ValueError):
                continue
        return total or None

    async def probe_window(self, window: TimeWindow, schemas: list[dict],
                           session_id: str = "") -> int | None:
        """0 行自检探针：统计该时间窗内首张表的原始行数，区分「真空数据 / 条件过窄」。

        只读、只返回计数，不参与结果计算；失败返回 None（自检降级不影响主链路）。
        """
        if not window:
            return None
        target = next((t for t in schemas or [] if "dt" in (t.get("columns") or [])), None)
        if not target:
            return None
        full = (f"{target['schema']}.{target['table_name']}" if target.get("schema")
                else target["table_name"])
        sql = (f"SELECT COUNT(*) AS c FROM {full} "
               f"WHERE dt BETWEEN '{window.start.isoformat()}' AND '{window.end.isoformat()}'")
        try:
            async with self._sem:
                return await asyncio.to_thread(self._count_sync, sql, session_id)
        except Exception as exc:  # noqa: BLE001 —— 探针失败只记日志
            logger.warning("时间窗探针失败：%s | SQL=%s", exc, sql)
            return None

    def _count_sync(self, sql: str, session_id: str) -> int:
        with self._engine_for(session_id).connect() as conn:
            return int(conn.execute(sa.text(sql)).scalar() or 0)

    def _execute_sync(self, sql: str, user_id: str, session_id: str = "",
                      window: TimeWindow | None = None) -> dict:
        eng = self._engine_for(session_id)
        with eng.connect() as conn:
            # 权限设计 §1.2：会话变量透传登录用户身份；数仓侧按此做行/列过滤
            try:
                conn.execute(sa.text(f"SET SESSION app_current_user = '{user_id}'"))
            except Exception:  # noqa: BLE001 —— 数仓无该会话变量时忽略（权限设计 §1.2 待评审项）
                pass
            # 执行超时（MySQL MAX_EXECUTION_TIME）
            ms = settings.exec_timeout_ms
            conn.execute(sa.text(f"SET SESSION max_execution_time = {ms}"))
            final_sql = inject_dates(sql, window)
            try:
                result = conn.execute(sa.text(final_sql))
                columns = list(result.keys())
                rows = [dict(zip(columns, r)) for r in result.fetchmany(settings.exec_row_limit)]
                truncated = len(rows) >= settings.exec_row_limit
            except sa.exc.SQLAlchemyError as exc:
                detail = str(getattr(exc, "orig", exc)).splitlines()[0]
                logger.warning("SQL 执行失败：%s | SQL=%s", detail, final_sql)
                raise DataProError(E006) from exc
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    async def fetch_all(self, sql: str, user_id: str, session_id: str = "",
                        window: TimeWindow | None = None,
                        max_rows: int | None = None) -> dict:
        """B13/F11：审批通过后取全量结果（受 export_max_rows 限制），供 CSV 导出。"""
        async with self._sem:
            return await asyncio.to_thread(self._fetch_all_sync, sql, user_id, session_id,
                                           window, max_rows or settings.export_max_rows)

    def _fetch_all_sync(self, sql: str, user_id: str, session_id: str,
                        window: TimeWindow | None, max_rows: int) -> dict:
        eng = self._engine_for(session_id)
        with eng.connect() as conn:
            try:
                conn.execute(sa.text(f"SET SESSION app_current_user = '{user_id}'"))
            except Exception:  # noqa: BLE001
                pass
            conn.execute(sa.text(f"SET SESSION max_execution_time = {max(settings.exec_timeout_ms, 60_000)}"))
            final_sql = inject_dates(sql, window)
            try:
                result = conn.execute(sa.text(final_sql))
                columns = list(result.keys())
                rows = [dict(zip(columns, r)) for r in result.fetchmany(max_rows)]
            except sa.exc.SQLAlchemyError as exc:
                detail = str(getattr(exc, "orig", exc)).splitlines()[0]
                logger.warning("导出查询失败：%s | SQL=%s", detail, final_sql)
                raise DataProError(E006) from exc
        return {"columns": columns, "rows": rows, "row_count": len(rows),
                "truncated": len(rows) >= max_rows}


executor_service = ExecutorService()
