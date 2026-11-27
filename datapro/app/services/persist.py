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

from app.db.models import AuditLog, Message, QueryRecord
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
    spec = getattr(ctx, "spec", None)
    with SessionLocal() as db:
        rec = QueryRecord(
            session_id=ctx.session_id or "",
            username=ctx.user_id or None,
            # 存用户原话，合并上下文单独落 question_context，避免多轮追问把原话覆盖掉
            question=ctx.raw_question or ctx.question,
            question_context=(json.dumps(ctx.question_context, ensure_ascii=False)
                              if ctx.question_context else None),
            recalled_schema=json.dumps(ctx.recalled_tables or [], ensure_ascii=False),
            generated_sql=ctx.generated_sql,
            validated=bool(ctx.generated_sql),
            executed=bool(result.get("columns")),
            row_count=int(result.get("row_count") or 0),
            latency_ms=latency_ms,
            final_answer=answer,
            # 结果分类与错误码：错误率统计、按 E00x 追溯的依据
            outcome=(getattr(ctx, "outcome", "") or "unknown")[:16],
            error_code=(getattr(ctx, "error_code", "") or None),
            spec_json=(json.dumps(spec.to_dict(), ensure_ascii=False)
                       if spec is not None and hasattr(spec, "to_dict") else None),
            sql_final=(getattr(ctx, "sql_final", None) or ctx.generated_sql),
            trace_id=ctx.trace_id or "",
            langfuse_trace_id=getattr(ctx, "langfuse_trace_id", "") or None,
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


def _save_message_sync(session_id: str, username: str, role: str, content: str,
                       kind: str, trace_id: str, attachments: str | None = None,
                       payload: str | None = None) -> int:
    with SessionLocal() as db:
        rec = Message(session_id=session_id, username=username or None, role=role,
                      kind=kind or None, content=content, trace_id=trace_id or "",
                      attachments=attachments, payload=payload)
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec.id


async def save_message(session_id: str, username: str, role: str, content: str,
                       kind: str = "query", trace_id: str = "",
                       attachments: list | None = None,
                       payload: dict | None = None) -> int | None:
    """全量消息落库（用户/助手，含闲聊与澄清轮）；失败只记日志，不影响对话。

    attachments 是附件引用快照（[{file_id,name,ext,size,origin}]），随消息持久化，
    供历史消息恢复后仍可点击预览；只存指针不存内容。
    payload 是终态事件快照（result/clarify/reject/escalate/error 的 SSE 事件），
    供跨设备恢复富结果（表格/KPI/依据/澄清卡片）。
    """
    if not _available() or not (content or "").strip():
        return None
    att_json = json.dumps(attachments, ensure_ascii=False) if attachments else None
    pay_json = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
    try:
        return await asyncio.to_thread(_save_message_sync, session_id, username, role,
                                       content, kind, trace_id, att_json, pay_json)
    except Exception as exc:  # noqa: BLE001 —— 旁路写入失败不阻断主链路
        _mark_failed(exc)
        return None


def _list_messages_sync(session_id: str, limit: int) -> list[dict]:
    """会话历史消息（升序），供前端恢复消息流（含附件引用与终态载荷）。"""
    with SessionLocal() as db:
        rows = (db.query(Message)
                .filter(Message.session_id == session_id)
                .order_by(Message.id.desc())
                .limit(limit)
                .all())
        rows.reverse()                       # 库里倒序取最新 N 条，返回按时间升序
        out = []
        for r in rows:
            out.append({
                "id": r.id, "role": r.role, "kind": r.kind,
                "content": r.content,
                "attachments": _parse_json(r.attachments) or [],
                "payload": _parse_json(r.payload),
                "trace_id": r.trace_id, "username": r.username,
                "created_at": _ts(r.created_at),
            })
        return out


def _parse_json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


async def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    if not _available():
        return []
    try:
        return await asyncio.to_thread(_list_messages_sync, session_id, max(1, min(limit, 500)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取会话历史失败：%s", exc)
        return []


def _session_has_user_sync(session_id: str, username: str) -> bool:
    with SessionLocal() as db:
        return db.query(Message.id).filter(
            Message.session_id == session_id,
            Message.username == username).first() is not None


async def session_has_user(session_id: str, username: str) -> bool:
    """该用户是否参与过此会话（owner 记录过期时的历史读取回退判定）。"""
    if not _available() or not username:
        return False
    try:
        return await asyncio.to_thread(_session_has_user_sync, session_id, username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话参与校验失败：%s", exc)
        return False


def _list_sessions_sync(username: str, limit: int) -> list[dict]:
    """服务端会话归档：本人参与过的会话列表（标题取本人首条提问，跨设备可见）。"""
    from sqlalchemy import func

    with SessionLocal() as db:
        rows = (db.query(Message.session_id,
                         func.count(Message.id),
                         func.max(Message.created_at))
                .filter(Message.username == username)
                .group_by(Message.session_id)
                .order_by(func.max(Message.created_at).desc())
                .limit(limit)
                .all())
        out = []
        for sid, cnt, last in rows:
            first = (db.query(Message.content)
                     .filter(Message.session_id == sid, Message.role == "user",
                             Message.username == username)
                     .order_by(Message.id.asc())
                     .first())
            out.append({
                "session_id": sid,
                "title": (first[0] or "历史会话")[:40] if first else "历史会话",
                "message_count": cnt,
                "last_at": _ts(last),
            })
        return out


async def list_sessions_for_user(username: str, limit: int = 100) -> list[dict]:
    if not _available() or not username:
        return []
    try:
        return await asyncio.to_thread(_list_sessions_sync, username, max(1, min(limit, 200)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取会话归档失败：%s", exc)
        return []


def merge_records(query_items: list[dict], message_items: list[dict],
                  page: int, page_size: int, total: int | None = None) -> tuple[list[dict], int]:
    """取数记录 + 全量消息按时间倒序合并分页（纯函数，便于单测）。

    total 由调用方用 COUNT 传入：两表各自只取了「当前页所需行数」，
    用 len(merged) 当总数会把 total 截断成 page*page_size。
    """
    merged = sorted(query_items + message_items,
                    key=lambda r: r.get("created_at") or "", reverse=True)
    start = max(page - 1, 0) * page_size
    return merged[start:start + page_size], (len(merged) if total is None else total)


def _list_records_sync(username: str, days: int, rating: str, keyword: str,
                       page: int, page_size: int, outcome: str = "",
                       error_code: str = "") -> tuple[list[dict], int]:
    """管理后台对话记录列表：query_record 与 message 合并，按时间倒序分页。"""
    from datetime import datetime, timedelta

    limit = max(page, 1) * page_size          # 每张表取够当前页所需的行数
    since = datetime.now() - timedelta(days=days) if days > 0 else None
    with SessionLocal() as db:
        q_stmt = select(QueryRecord)
        if username:
            q_stmt = q_stmt.where(QueryRecord.username == username)
        if since is not None:
            q_stmt = q_stmt.where(QueryRecord.created_at >= since)
        if rating in ("up", "down"):
            q_stmt = q_stmt.where(QueryRecord.user_feedback == rating)
        if keyword:
            q_stmt = q_stmt.where(QueryRecord.question.like(f"%{keyword}%"))
        if outcome:
            q_stmt = q_stmt.where(QueryRecord.outcome == outcome)
        if error_code:
            q_stmt = q_stmt.where(QueryRecord.error_code == error_code)

        m_stmt = select(Message)
        if username:
            m_stmt = m_stmt.where(Message.username == username)
        if since is not None:
            m_stmt = m_stmt.where(Message.created_at >= since)
        if rating in ("up", "down") or outcome or error_code:
            from sqlalchemy import false as sa_false

            m_stmt = m_stmt.where(sa_false())      # 反馈/结果分类只存在于取数记录
        if keyword:
            m_stmt = m_stmt.where(Message.content.like(f"%{keyword}%"))

        total = _count(db, q_stmt) + _count(db, m_stmt)
        q_rows = db.execute(q_stmt.order_by(QueryRecord.id.desc()).limit(limit)).scalars().all()
        m_rows = db.execute(m_stmt.order_by(Message.id.desc()).limit(limit)).scalars().all()

    query_items = [{
        "id": r.id, "source": "query", "kind": "query", "session_id": r.session_id,
        "username": r.username, "question": r.question,
        "question_context": json.loads(r.question_context) if r.question_context else [],
        "sql": r.generated_sql, "row_count": r.row_count, "latency_ms": r.latency_ms,
        "answer": r.final_answer, "feedback": r.user_feedback,
        "feedback_reason": r.feedback_reason, "trace_code": f"DP-{r.id:04d}",
        "outcome": r.outcome, "error_code": r.error_code,
        "created_at": _ts(r.created_at),
    } for r in q_rows]
    message_items = [{
        "id": r.id, "source": "message", "kind": r.kind or r.role, "session_id": r.session_id,
        "username": r.username, "question": r.content if r.role == "user" else "",
        "question_context": [], "sql": None, "row_count": None, "latency_ms": None,
        "answer": r.content if r.role == "assistant" else "", "feedback": None,
        "feedback_reason": None, "trace_code": "",
        "outcome": None, "error_code": None,
        "created_at": _ts(r.created_at),
    } for r in m_rows]
    return merge_records(query_items, message_items, page, page_size, total=total)


def _ts(value) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _count(db, stmt) -> int:
    from sqlalchemy import func, select as sa_select

    return int(db.execute(sa_select(func.count()).select_from(stmt.subquery())).scalar_one())


async def list_records(username: str = "", days: int = 7, rating: str = "",
                       keyword: str = "", page: int = 1,
                       page_size: int = 20, outcome: str = "",
                       error_code: str = "") -> tuple[list[dict], int]:
    if not _available():
        return [], 0
    try:
        return await asyncio.to_thread(_list_records_sync, username, days, rating,
                                       keyword, page, page_size, outcome, error_code)
    except Exception as exc:  # noqa: BLE001 —— 后台查询失败只降级为空列表
        _mark_failed(exc)
        return [], 0


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


def _snapshot_sync(query_id: str) -> dict | None:
    """取一条取数记录的快照，供 👍 候选池使用（D04）。"""
    with SessionLocal() as db:
        if query_id == "latest":
            rec = db.execute(select(QueryRecord).order_by(QueryRecord.id.desc()).limit(1)
                             ).scalar_one_or_none()
        else:
            rec = db.get(QueryRecord, int(query_id))
        if rec is None:
            return None
        return {"query_id": rec.id, "username": rec.username,
                "question": rec.question,
                "question_context": rec.question_context, "sql": rec.generated_sql,
                "row_count": rec.row_count, "feedback": rec.user_feedback,
                "feedback_reason": rec.feedback_reason, "trace_id": rec.trace_id,
                "created_at": rec.created_at.isoformat() if rec.created_at else None}


async def query_snapshot(query_id: str) -> dict | None:
    if not _available():
        return None
    try:
        return await asyncio.to_thread(_snapshot_sync, query_id)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(exc)
        return None


def _langfuse_trace_sync(query_id: str) -> str | None:
    with SessionLocal() as db:
        if query_id == "latest":
            rec = db.execute(select(QueryRecord).order_by(QueryRecord.id.desc()).limit(1)
                             ).scalar_one_or_none()
        else:
            rec = db.get(QueryRecord, int(query_id))
        return rec.langfuse_trace_id if rec else None


async def langfuse_trace_id(query_id: str) -> str | None:
    """反馈打分用：回查该记录对应的 Langfuse trace。"""
    if not _available():
        return None
    try:
        return await asyncio.to_thread(_langfuse_trace_sync, query_id)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(exc)
        return None


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


# ---------------- 生命周期管理：按保留期分批清理 ----------------

_PURGE_BATCH = 5000


def _purge_table_sync(db, table: str, cutoff, batch: int) -> int:
    """分批删除，避免一条 DELETE 长时间持锁；SQL 注入面由调用方固定表名控制。"""
    from sqlalchemy import text

    total = 0
    while True:
        n = db.execute(text(f"DELETE FROM `{table}` WHERE created_at < :c LIMIT :n"),
                       {"c": cutoff, "n": batch}).rowcount or 0
        db.commit()
        total += n
        if n < batch:
            return total


def purge_expired_sync(audit_days: int, query_days: int, message_days: int = 0,
                       batch: int = _PURGE_BATCH) -> dict:
    """按保留期清理审计、取数记录与消息，返回各表删除行数。

    为什么需要：audit_log 每次提问写多条，message 每轮两条，只增不减；
    半年后单表查询与备份都会变慢。清理依赖 created_at 索引，避免全表扫。
    """
    from datetime import datetime, timedelta

    stats: dict[str, int] = {}
    now = datetime.now()
    with SessionLocal() as db:
        for table, days in (("audit_log", audit_days),
                            ("query_record", query_days),
                            ("message", message_days)):
            if days > 0:
                stats[table] = _purge_table_sync(db, table, now - timedelta(days=days), batch)
    return stats


async def purge_expired(audit_days: int, query_days: int, message_days: int = 0) -> dict:
    """清理过期记录（旁路任务：失败只记日志，不影响主链路）。"""
    if not _available():
        return {}
    try:
        return await asyncio.to_thread(purge_expired_sync, audit_days, query_days, message_days)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(exc)
        logger.warning("过期数据清理失败：%s", exc)
        return {}
