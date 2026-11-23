"""只读执行引擎（HLD §3）：独立连接池、会话级数据库身份、超时、行数截断。"""
import asyncio
import re
import threading
from datetime import date, timedelta

import sqlalchemy as sa

from app.config import settings
from app.services.db_identity import session_db_url
from app.services.session_registry import session_registry

# 时间占位符（Prompt设计 §3.1 规则 4）：日报表 T-1 为最新可用分区
_PLACEHOLDER = re.compile(r"\{(date|start_date|end_date)\}")
MAX_SESSION_ENGINES = 50


def inject_dates(sql: str, today: date | None = None) -> str:
    """注入 {date}/{start_date}/{end_date}：默认最近 7 天（T-7 ~ T-1）。"""
    end = (today or date.today()) - timedelta(days=1)
    mapping = {
        "date": end.isoformat(),
        "start_date": (end - timedelta(days=6)).isoformat(),
        "end_date": end.isoformat(),
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

    def _engine_for(self, session_id: str) -> sa.Engine:
        """会话级账号：仅能读公共数仓 + 本会话文件表（安全修复）。"""
        if not session_id:
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

    async def execute(self, sql: str, user_id: str, session_id: str = "") -> dict:
        if session_id:
            session_registry.touch(session_id)  # S07：查询即视为活跃
        async with self._sem:
            return await asyncio.to_thread(self._execute_sync, sql, user_id, session_id)

    def _execute_sync(self, sql: str, user_id: str, session_id: str = "") -> dict:
        eng = self._engine_for(session_id)
        with eng.connect() as conn:
            # 权限设计 §1.2：会话变量透传登录用户身份；数仓侧按此做行/列过滤
            try:
                conn.execute(sa.text(f"SET SESSION app_current_user = '{user_id}'"))
            except Exception:  # 数仓无该会话变量时忽略（权限设计 §1.2 待评审项）
                pass
            # 执行超时（MySQL MAX_EXECUTION_TIME）
            ms = settings.exec_timeout_ms
            conn.execute(sa.text(f"SET SESSION max_execution_time = {ms}"))
            result = conn.execute(sa.text(inject_dates(sql)))
            columns = list(result.keys())
            rows = [dict(zip(columns, r)) for r in result.fetchmany(settings.exec_row_limit)]
            truncated = len(rows) >= settings.exec_row_limit
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


executor_service = ExecutorService()
