"""导出申请与审批接口（B13/F11）。"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.auth import AuthUser, require_admin, require_permission

logger = logging.getLogger("datapro.export")
router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


class ExportIn(BaseModel):
    question: str
    sql: str
    session_id: str = ""
    row_count: int | None = None
    reason: str = ""
    window_start: str | None = None
    window_end: str | None = None


class DecisionIn(BaseModel):
    note: str = ""


def _validate_export_sql(sql: str) -> None:
    """导出申请必须先过与主链路同一套 SQL 校验（只读/白名单/LIMIT/危险函数）。"""
    from app.services.metadata_store import metadata_store
    from app.services.validator import validate_sql

    allowed = {str(t["table_name"]).lower() for t in metadata_store.tables()}
    result = validate_sql(sql, allowed)
    if not result.passed:
        raise HTTPException(status_code=400, detail="导出 SQL 未通过校验：" + "；".join(result.errors))


@router.post("")
async def create_export(payload: ExportIn, user: AuthUser = Depends(require_permission("chat.use"))):
    from app.services.export_registry import export_registry
    from app.services.session_state import session_state

    _validate_export_sql(payload.sql)
    # 会话归属：禁止用他人的 session_id 申请导出以读取其上传文件表
    if payload.session_id:
        owner = await session_state.owner(payload.session_id)
        if owner and owner != user.username and user.role != "admin":
            raise HTTPException(status_code=403, detail="无权基于该会话申请导出")
    try:
        return export_registry.create(user.username, payload.question, payload.sql,
                                      payload.session_id, payload.row_count, payload.reason,
                                      payload.window_start, payload.window_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
async def list_exports(all: int = 0, status: str | None = None,
                       user: AuthUser = Depends(require_permission("chat.use"))):
    from app.services.export_registry import export_registry
    is_admin = user.role == "admin"
    return export_registry.list(user.username, all_users=bool(all and is_admin), status=status)


@router.post("/{request_id}/approve")
async def approve(request_id: str, payload: DecisionIn,
                  user: AuthUser = Depends(require_admin)):
    from app.services.export_registry import export_registry
    try:
        export_registry.decide(request_id, "approve", user.username, payload.note)
        return await export_registry.run_export(request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="申请不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{request_id}/reject")
async def reject(request_id: str, payload: DecisionIn,
                 user: AuthUser = Depends(require_admin)):
    from app.services.export_registry import export_registry
    try:
        return export_registry.decide(request_id, "reject", user.username, payload.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="申请不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{request_id}/download")
async def download(request_id: str, user: AuthUser = Depends(require_permission("chat.use"))):
    from app.services.export_registry import export_registry
    record = export_registry.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="申请不存在")
    if user.role != "admin" and record.get("requester") != user.username:
        raise HTTPException(status_code=403, detail="无权下载该导出")
    path = export_registry.file_path(request_id)
    if not path:
        raise HTTPException(status_code=404, detail="导出文件尚未生成")
    _audit_download(user.username, record)
    return FileResponse(path, media_type="text/csv",
                        filename=f"{request_id}.csv")


def _audit_download(username: str, record: dict) -> None:
    """下载留痕：写 audit_log，失败只记日志，不影响下载。"""
    try:
        from app.core.trace import current_trace_id
        from app.db.models import AuditLog
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            db.add(AuditLog(
                trace_id=current_trace_id(), user_id=username, node="EXPORT",
                question_digest=f"下载导出 {record.get('id')}"[:255],
                sql_text=record.get("sql"),
                payload=json.dumps({
                    "request_id": record.get("id"),
                    "requester": record.get("requester"),
                    "row_count": record.get("row_count"),
                    "file": record.get("file"),
                }, ensure_ascii=False),
            ))
            db.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计旁路失败不阻断下载
        logger.warning("导出下载审计失败：%s", exc)
