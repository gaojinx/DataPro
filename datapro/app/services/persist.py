"""查询记录与审计落库（功能清单 B08 / S03）。

- 写入自有业务库 `app_database_url`（query_record / audit_log）
- 任何失败只记日志，不影响主链路；失败后 60 秒内不再尝试，避免拖慢请求
- SQLAlchemy 同步写放到线程池，避免阻塞事件循环
"""
import asyncio
import json
import logging
import time

from sqlalchemy import select

from app.db.models import AuditLog, QueryRecord
from app.db.session import SessionLocal

logger = logging.getLogger("datapro.persist")

_RETRY_SECONDS = 60
_retry_after = 0.0
_bg_tasks: set[asyncio.Task] = set()


def _available() -> bool:
    return time.monotonic() >= _retry_after


def _mark_failed(exc: Exception) -> None:
    global _retry_after
    _retry_after = time.monotonic() + _RETRY_SECONDS
    logger.warning("业务库写入失败，%s 秒内跳过：%s", _RETRY_SECONDS, exc)


def spawn(coro) -> None:
    """后台任务（保留引用，避免被回收）。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _save_query_sync(ctx, answer: str, latency_ms: int) -> int:
    result = ctx.result or {}
    with SessionLocal() as db:
        rec = QueryRecord(
            session_id=ctx.session_id or "",
            question=ctx.question,
            recalled_schema=json.dumps(ctx.recalled_tables or [], ensure_ascii=False),
            generated_sql=ctx.generated_sql,
            validated=bool(ctx.generated_sql),
            executed=bool(result.get("columns")),
            row_count=int(result.get("row_count") or 0),
            latency_ms=latency_ms,
            final_answer=answer,
            trace_id=ctx.trace_id or "",
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec.id


async def save_query(ctx, answer: str, latency_ms: int) -> int | None:
    """保存一次取数记录，返回 query_id（失败返回 None）。"""
    if not _available():
        return None
    try:
        return await asyncio.to_thread(_save_query_sync, ctx, answer, latency_ms)
    except Exception as exc:  # noqa: BLE001 —— 持久化失败不阻断主链路
        _mark_failed(exc)
        return None


def _set_feedback_sync(query_id: str, rating: str, reason: str | None) -> bool:
    with SessionLocal() as db:
        if query_id == "latest":
            rec = db.execute(
                select(QueryRecord).order_by(QueryRecord.id.desc()).limit(1)
            ).scalar_one_or_none()
        else:
            rec = db.get(QueryRecord, int(query_id))
        if rec is None:
            return False
        rec.user_feedback = rating
        if reason:
            rec.feedback_reason = reason[:64]
        db.commit()
        return True


async def set_feedback(query_id: str, rating: str, reason: str | None = None) -> bool:
    if not _available():
        return False
    try:
        return await asyncio.to_thread(_set_feedback_sync, query_id, rating, reason)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(exc)
        return False


def _save_audit_sync(entry: dict) -> None:
    with SessionLocal() as db:
        db.add(AuditLog(
            trace_id=entry.get("trace_id", ""),
            user_id=entry.get("user_id", ""),
            node=entry.get("node", ""),
            question_digest=entry.get("question_digest", "")[:255],
            sql_text=entry.get("sql_text"),
            payload=entry.get("payload"),
        ))
        db.commit()


async def save_audit(entry: dict) -> None:
    """审计旁路写入：失败只记日志。"""
    if not _available():
        return
    try:
        await asyncio.to_thread(_save_audit_sync, entry)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(exc)
