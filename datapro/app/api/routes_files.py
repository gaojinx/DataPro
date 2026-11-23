"""会话文件接口（B18/F27）：上传后自动解析可查询文件为临时表。"""
import asyncio

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.services.file_ingest import ingest_service
from app.services.file_store import file_store

router = APIRouter(prefix="/api/v1/files", tags=["files"])

PARSE_EXTS = {".csv", ".tsv", ".json", ".xlsx"}


@router.post("")
async def upload_file(file: UploadFile = File(...), session_id: str = Form("default"),
                      origin: str = Form("upload"), query_id: str | None = Form(None)):
    content = await file.read()
    try:
        entry = file_store.add(session_id=session_id, filename=file.filename or "file",
                               content=content, origin=origin, query_id=query_id)
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
async def list_files(session_id: str = "default"):
    return {"files": file_store.list(session_id)}


@router.get("/{file_id}/download")
async def download_file(file_id: str):
    entry = file_store.get(file_id)
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        path = file_store.path_of(entry)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路径")
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path, media_type=entry["mime"], filename=entry["name"])


@router.delete("/{file_id}")
async def delete_file(file_id: str):
    entry = file_store.get(file_id)
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")
    # 先清理临时表与授权，再删文件记录
    await asyncio.to_thread(ingest_service.drop_table, file_id, entry["session_id"])
    file_store.delete(file_id)
    return {"deleted": file_id}
