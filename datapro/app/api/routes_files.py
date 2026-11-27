"""会话文件接口（B18/F27）：上传后自动解析可查询文件为临时表。

鉴权（B12）：上传/列表/预览/下载/锁定/删除全部要求登录，并按上传者校验文件归属；
预览与下载走浏览器原生请求（a/img/iframe），额外支持 `?token=` 传 Token。
"""
import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.auth import (AuthUser, get_current_user, get_current_user_flexible,
                               require_permission)
from app.services.executor import executor_service
from app.services.file_ingest import ingest_service
from app.services.file_lock import acquire as acquire_lock, release as release_lock
from app.services.file_store import file_store

router = APIRouter(prefix="/api/v1/files", tags=["files"])

PARSE_EXTS = {".csv", ".tsv", ".json", ".xlsx"}


class LockRequest(BaseModel):
    owner: str


def _can_access(entry: dict, user: AuthUser) -> bool:
    """管理员可访问全部；普通用户仅可访问自己上传或历史无归属的文件。"""
    if user.role == "admin":
        return True
    owner = entry.get("owner")
    return not owner or owner == user.username


def _require_access(entry: dict, user: AuthUser) -> None:
    if not _can_access(entry, user):
        raise HTTPException(status_code=403, detail="无权访问该文件")


def _get_entry(file_id: str, user: AuthUser) -> dict:
    entry = file_store.get(file_id)
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")
    _require_access(entry, user)
    return entry


@router.post("")
async def upload_file(file: UploadFile = File(...), session_id: str = Form("default"),
                      origin: str = Form("upload"), query_id: str | None = Form(None),
                      root_id: str | None = Form(None),
                      user: AuthUser = Depends(require_permission("files.upload"))):
    content = await file.read()
    try:
        entry = file_store.add(session_id=session_id, filename=file.filename or "file",
                               content=content, origin=origin, query_id=query_id,
                               owner=user.username, root_id=root_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # CSV/XLSX 自动解析入库；失败不影响上传成功（前端可展示原因）
    if entry["ext"] in PARSE_EXTS:
        try:
            await asyncio.to_thread(ingest_service.ingest, entry["file_id"])
            entry = file_store.get(entry["file_id"]) or entry
            entry = {k: v for k, v in entry.items() if k != "stored_name"}
        except Exception as e:  # 解析失败：记录原因，不影响文件本身
            file_store.update(entry["file_id"], {"parse_error": str(e)[:200]})
            entry["parse_error"] = str(e)[:200]
    return entry


@router.get("")
async def list_files(session_id: str = "default", user: AuthUser = Depends(get_current_user)):
    return {"files": [f for f in file_store.list(session_id) if _can_access(f, user)]}


@router.get("/{file_id}/download")
async def download_file(file_id: str, inline: bool = False,
                        user: AuthUser = Depends(get_current_user_flexible)):
    entry = _get_entry(file_id, user)
    try:
        path = file_store.path_of(entry)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路径")
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path, media_type=entry["mime"], filename=entry["name"],
                        content_disposition_type="inline" if inline else "attachment")


def _public_entry(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "stored_name"}


@router.get("/{file_id}/preview")
async def preview_file(file_id: str, limit: int = 200,
                       user: AuthUser = Depends(get_current_user_flexible)):
    """文件预览：已解析文件按工作表返回前 N 行；图片/PDF 由前端直连下载接口内联渲染。"""
    entry = _get_entry(file_id, user)
    parsed = entry.get("parsed") or {}
    tables = parsed.get("tables") or []
    if not tables:
        return {"file": _public_entry(entry), "kind": "raw", "sheets": []}

    limit = max(1, min(limit, 5000))
    sheets = []
    for t in tables:
        res = await executor_service.execute(
            f"SELECT * FROM `dp_uploads`.`{t['table']}` LIMIT {limit}",
            "preview", entry["session_id"])
        sheets.append({
            "table": t["table"],
            "sheet": t.get("sheet") or "工作表",
            "labels": t.get("labels") or res["columns"],
            "columns": res["columns"],
            "row_count": t.get("row_count", res["row_count"]),
            "rows": res["rows"],
        })
    return {"file": _public_entry(entry), "kind": "table", "sheets": sheets, "limit": limit}


@router.post("/{file_id}/lock")
async def lock_file(file_id: str, body: LockRequest, user: AuthUser = Depends(get_current_user)):
    _get_entry(file_id, user)
    ok, message = acquire_lock(file_id, user.username)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "ttl": 300}


@router.post("/{file_id}/unlock")
async def unlock_file(file_id: str, body: LockRequest, user: AuthUser = Depends(get_current_user)):
    _get_entry(file_id, user)
    # owner 只认认证主体：客户端声明的 body.owner 不再作为解锁凭据
    release_lock(file_id, user.username)
    return {"ok": True}


@router.get("/{file_id}/versions")
async def list_versions(file_id: str, user: AuthUser = Depends(get_current_user)):
    _get_entry(file_id, user)
    return {"versions": file_store.versions(file_id)}


@router.get("/{file_id}/diff")
async def diff_versions(file_id: str, against: str,
                        user: AuthUser = Depends(get_current_user)):
    """对比当前文件与指定历史版本（单元格级 diff）。"""
    _get_entry(file_id, user)
    _get_entry(against, user)
    from app.services.file_diff import compare
    try:
        return await asyncio.to_thread(compare, file_id, against)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{file_id}/restore")
async def restore_version(file_id: str,
                          user: AuthUser = Depends(require_permission("files.upload"))):
    """把历史版本恢复为一个新版本（原件保留，版本号继续递增）。"""
    entry = _get_entry(file_id, user)
    content = await asyncio.to_thread(file_store.content_of, entry)
    restored = file_store.add(session_id=entry["session_id"], filename=entry["name"],
                              content=content, origin="edited", owner=user.username,
                              root_id=entry.get("root_id") or entry["file_id"])
    if restored["ext"] in PARSE_EXTS:
        try:
            await asyncio.to_thread(ingest_service.ingest, restored["file_id"])
            restored = file_store.get(restored["file_id"]) or restored
        except Exception as e:  # noqa: BLE001 —— 解析失败不影响恢复文件
            file_store.update(restored["file_id"], {"parse_error": str(e)[:200]})
    return _public_entry(restored)


@router.delete("/{file_id}")
async def delete_file(file_id: str, user: AuthUser = Depends(require_permission("files.delete"))):
    entry = _get_entry(file_id, user)
    # 先清理临时表与授权，再删文件记录
    await asyncio.to_thread(ingest_service.drop_table, file_id, entry["session_id"])
    file_store.delete(file_id)
    return {"deleted": file_id}
