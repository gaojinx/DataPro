"""大结果集导出申请与审批（B13/F11）。

流程：用户对结果发起导出申请（pending）→ 管理员审批（approved/rejected）
→ 审批通过后执行只读查询、落 CSV 到 `data/exports/` → 申请人/管理员下载。
记录优先存业务库 `export_request`（多实例可见、可审计）；业务库不可用时降级
`data/export_requests.json`，降级后本次进程保持 JSON 模式，避免两边数据分裂。
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger("datapro.export")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
INDEX_PATH = _DATA_DIR / "export_requests.json"
EXPORT_DIR = _DATA_DIR / "exports"
MAX_REQUESTS = 500
_DB_BROKEN = False


def _fmt(dt) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) else dt


def _fmt_date(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else value


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _read_json() -> dict:
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"items": []}
    except Exception:  # noqa: BLE001 —— 文件缺失/损坏时从空开始
        return {"items": []}


def _write_json(payload: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_PATH)


def _to_dict(row) -> dict:
    return {
        "id": row.id, "requester": row.requester, "question": row.question,
        "sql": row.sql_text, "session_id": row.session_id,
        "window_start": _fmt_date(row.window_start), "window_end": _fmt_date(row.window_end),
        "preview_rows": row.preview_rows, "reason": row.reason, "status": row.status,
        "created_at": _fmt(row.created_at), "reviewed_by": row.reviewed_by,
        "reviewed_at": _fmt(row.reviewed_at), "review_note": row.review_note,
        "file": row.file, "row_count": row.row_count, "truncated": bool(row.truncated),
        "error": row.error, "exported_at": _fmt(row.exported_at),
        "duration_ms": row.duration_ms,
    }


class ExportRegistry:
    def __init__(self, use_db: bool = True):
        self._lock = threading.RLock()
        self._use_db = use_db

    # ---------- 存储选择 ----------
    def _db(self):
        """返回 SessionLocal；不可用或已降级时返回 None。"""
        global _DB_BROKEN
        if not self._use_db or _DB_BROKEN:
            return None
        try:
            from app.db.session import SessionLocal
            return SessionLocal
        except Exception as exc:  # noqa: BLE001 —— 导入失败直接走 JSON
            logger.warning("导出记录降级 JSON（业务库不可用）：%s", exc)
            _DB_BROKEN = True
            return None

    # ---------- 申请 ----------
    @staticmethod
    def _next_no(db) -> str:
        """业务号原子分配。

        原实现 `1 + max(尾部数字)` 需要全表扫描，且并发申请会算出同一个号，
        撞主键后抛 IntegrityError 进而触发整模块 JSON 降级。改为单行序列更新：
        O(1)、行锁保证唯一。
        """
        from sqlalchemy import text

        db.execute(text(
            "INSERT INTO id_sequence (name, next_val) VALUES ('export_request', LAST_INSERT_ID(1)) "
            "ON DUPLICATE KEY UPDATE next_val = LAST_INSERT_ID(next_val + 1)"))
        val = db.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
        return f"EP-{int(val):04d}"

    def create(self, requester: str, question: str, sql: str, session_id: str = "",
               row_count: int | None = None, reason: str = "",
               window_start: str | None = None, window_end: str | None = None) -> dict:
        if not sql.strip():
            raise ValueError("缺少可导出的查询 SQL")
        Session = self._db()
        if Session is not None:
            try:
                from app.db.models import ExportRequest
                with Session() as db:
                    row = ExportRequest(
                        id=self._next_no(db), requester=requester, question=question,
                        sql_text=sql, session_id=session_id, preview_rows=row_count,
                        reason=reason, status="pending",
                        window_start=_parse_date(window_start),
                        window_end=_parse_date(window_end))
                    db.add(row)
                    db.commit()
                    db.refresh(row)
                    return _to_dict(row)
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级 JSON
                self._degrade(exc)
        with self._lock:
            data = _read_json()
            items = data.get("items") or []
            next_no = 1 + max([int(str(i.get("id", "EP-0")).split("-")[-1] or 0)
                               for i in items] or [0])
            record = {
                "id": f"EP-{next_no:04d}", "requester": requester, "question": question,
                "sql": sql, "session_id": session_id, "preview_rows": row_count,
                "reason": reason, "status": "pending",
                "window_start": window_start, "window_end": window_end,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reviewed_by": None, "reviewed_at": None, "review_note": "",
                "file": None, "row_count": None, "truncated": False,
                "error": None, "exported_at": None, "duration_ms": None,
            }
            items.insert(0, record)
            _write_json({"updated_at": record["created_at"], "items": items[:MAX_REQUESTS]})
            return record

    # ---------- 查询 ----------
    def list(self, requester: str | None = None, all_users: bool = False,
             status: str | None = None) -> list[dict]:
        Session = self._db()
        if Session is not None:
            try:
                from app.db.models import ExportRequest
                with Session() as db:
                    q = db.query(ExportRequest)
                    if not all_users and requester:
                        q = q.filter(ExportRequest.requester == requester)
                    if status:
                        q = q.filter(ExportRequest.status == status)
                    return [_to_dict(r) for r in
                            q.order_by(ExportRequest.created_at.desc()).limit(MAX_REQUESTS).all()]
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        items = _read_json().get("items") or []
        if not all_users and requester:
            items = [i for i in items if i.get("requester") == requester]
        if status:
            items = [i for i in items if i.get("status") == status]
        return items

    def get(self, request_id: str) -> dict | None:
        Session = self._db()
        if Session is not None:
            try:
                from app.db.models import ExportRequest
                with Session() as db:
                    row = db.get(ExportRequest, request_id)
                    return _to_dict(row) if row else None
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        return next((i for i in _read_json().get("items") or []
                     if i.get("id") == request_id), None)

    # ---------- 审批 ----------
    def decide(self, request_id: str, action: str, reviewer: str, note: str = "") -> dict:
        if action not in ("approve", "reject"):
            raise ValueError("action 只能是 approve 或 reject")
        status = "approved" if action == "approve" else "rejected"
        Session = self._db()
        if Session is not None:
            try:
                from app.db.models import ExportRequest
                with Session() as db:
                    row = db.get(ExportRequest, request_id)
                    if row is None:
                        raise KeyError(request_id)
                    if row.status != "pending":
                        raise ValueError(f"申请已是 {row.status} 状态，不能重复审批")
                    row.status = status
                    row.reviewed_by = reviewer
                    row.reviewed_at = datetime.now()
                    row.review_note = note
                    db.commit()
                    db.refresh(row)
                    return _to_dict(row)
            except (KeyError, ValueError):
                raise
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        with self._lock:
            data = _read_json()
            record = next((i for i in data.get("items") or []
                           if i.get("id") == request_id), None)
            if record is None:
                raise KeyError(request_id)
            if record["status"] != "pending":
                raise ValueError(f"申请已是 {record['status']} 状态，不能重复审批")
            record.update({"status": status, "reviewed_by": reviewer,
                           "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "review_note": note})
            data["updated_at"] = record["reviewed_at"]
            _write_json(data)
            return record

    # ---------- 执行导出 ----------
    async def run_export(self, request_id: str) -> dict:
        """审批通过后执行查询并写 CSV；失败标记 failed，不抛出到接口层。"""
        record = self.get(request_id)
        if record is None:
            raise KeyError(request_id)
        if record["status"] not in ("approved", "failed"):
            raise ValueError(f"申请状态为 {record['status']}，不能导出")
        from app.services.executor import executor_service

        # A02：导出是绕过状态机的执行路径，按申请人**当前**数据表权限复核
        # （申请后权限可能被收回）；越权直接标记失败，不执行 SQL
        from app.services.db_access import db_access
        from app.services.metadata_store import metadata_store
        from app.services.validator import validate_sql

        user_tables = db_access.tables_for(record["requester"], refresh=True)
        allowed = {str(t["table_name"]).lower() for t in metadata_store.tables()}
        # 无条件校验：即使未配置用户级表权限，也必须过只读/白名单/危险函数检查
        check = validate_sql(record["sql"], allowed, user_tables=user_tables)
        if check.forbidden:
            self._update(request_id, {
                "status": "failed",
                "error": f"申请人当前无权访问数据表：{', '.join(check.forbidden)}",
            })
            return self.get(request_id) or record
        if not check.passed:
            self._update(request_id, {
                "status": "failed",
                "error": "导出 SQL 未通过校验：" + "；".join(check.errors),
            })
            return self.get(request_id) or record

        window = None
        if record.get("window_start") and record.get("window_end"):
            from app.core.time_window import TimeWindow
            start, end = _parse_date(record["window_start"]), _parse_date(record["window_end"])
            if start and end:
                window = TimeWindow(start=start, end=end, label="导出原查询时间窗")

        started = time.time()
        try:
            result = await executor_service.fetch_all(
                record["sql"], record["requester"], record.get("session_id") or "",
                window=window)
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = EXPORT_DIR / f"{request_id}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=result["columns"])
                writer.writeheader()
                for row in result["rows"]:
                    writer.writerow(row)
            self._update(request_id, {
                "status": "approved", "file": path.name, "row_count": result["row_count"],
                "truncated": result["truncated"], "error": None,
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "duration_ms": int((time.time() - started) * 1000),
            })
        except Exception as exc:  # noqa: BLE001 —— 导出失败写回记录，接口仍返回记录
            logger.warning("导出执行失败 %s：%s", request_id, exc)
            self._update(request_id, {"status": "failed", "error": str(exc)[:300]})
        return self.get(request_id) or record

    def file_path(self, request_id: str) -> Path | None:
        record = self.get(request_id)
        if not record or not record.get("file"):
            return None
        path = EXPORT_DIR / record["file"]
        return path if path.exists() else None

    # ---------- 内部 ----------
    def _update(self, request_id: str, fields: dict) -> None:
        Session = self._db()
        if Session is not None:
            try:
                from app.db.models import ExportRequest
                with Session() as db:
                    row = db.get(ExportRequest, request_id)
                    if row is None:
                        raise KeyError(request_id)
                    for key, value in fields.items():
                        if key == "sql":
                            key = "sql_text"
                        if hasattr(row, key):
                            setattr(row, key, value)
                    db.commit()
                    return
            except KeyError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
        with self._lock:
            data = _read_json()
            for item in data.get("items") or []:
                if item.get("id") == request_id:
                    item.update(fields)
                    break
            data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_json(data)

    @staticmethod
    def _degrade(exc: Exception) -> None:
        global _DB_BROKEN
        if not _DB_BROKEN:
            logger.warning("导出记录读写失败，降级 JSON：%s", exc)
            _DB_BROKEN = True


export_registry = ExportRegistry()
