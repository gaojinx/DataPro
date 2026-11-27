"""数据表权限自助申请接口（U-B05）。

- 用户：提交申请、查看自己的申请进度；
- 管理员：查看全部申请、审批（通过即按有效期写入 db_access）。
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, require_admin, require_permission

logger = logging.getLogger("datapro.access")
router = APIRouter(tags=["access"])


class AccessIn(BaseModel):
    table_name: str
    reason: str = ""
    duration_days: int = 30


class DecisionIn(BaseModel):
    note: str = ""
    duration_days: int | None = None


def _unavailable(exc: Exception) -> HTTPException:
    logger.warning("权限申请接口异常：%s", exc)
    return HTTPException(status_code=503, detail="权限申请服务暂不可用，请联系管理员")


@router.get("/api/v1/access/catalog")
async def catalog(_: AuthUser = Depends(require_permission("chat.use"))):
    """可申请的数据表清单：只给表名与中文名，不含字段结构。

    为什么普通用户也能读：申请的前提是「知道自己要哪张表」；只暴露表名不构成数据访问，
    真正的数据边界仍由 A02 + SQL 校验 + 只读账号三道防线守。
    """
    from app.services.metadata_store import metadata_store

    items = [{"table_name": str(t.get("table_name") or ""),
              # 元数据字典用 description 承载中文名；兼容历史/种子里的 cn_name
              "cn_name": str(t.get("cn_name") or t.get("description") or "")}
             for t in metadata_store.tables() if t.get("table_name")]
    items.sort(key=lambda x: x["table_name"])
    return {"items": items}


@router.post("/api/v1/access/requests")
async def create_request(payload: AccessIn,
                         user: AuthUser = Depends(require_permission("chat.use"))):
    from app.services import access_requests

    try:
        return await asyncio.to_thread(access_requests.create, user.username,
                                       payload.table_name, payload.reason,
                                       payload.duration_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 业务库不可用时不假成功
        raise _unavailable(exc) from exc


@router.get("/api/v1/access/requests")
async def my_requests(status: str = "", limit: int = 50,
                      user: AuthUser = Depends(require_permission("chat.use"))):
    from app.services import access_requests

    try:
        items = await asyncio.to_thread(access_requests.list_requests,
                                        user.username, status.strip() or None, limit)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return {"items": items, "total": len(items)}


@router.get("/admin/access/requests")
async def list_requests(status: str = "", limit: int = 200,
                        _: AuthUser = Depends(require_admin)):
    from app.services import access_requests

    try:
        items = await asyncio.to_thread(access_requests.list_requests,
                                        None, status.strip() or None, limit)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return {"items": items, "total": len(items)}


@router.post("/admin/access/requests/{request_id}/approve")
async def approve(request_id: str, payload: DecisionIn,
                  user: AuthUser = Depends(require_admin)):
    from app.services import access_requests

    try:
        return await asyncio.to_thread(access_requests.decide, request_id, "approve",
                                       user.username, payload.note, payload.duration_days)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 授权写库失败必须显式报错
        raise _unavailable(exc) from exc


@router.post("/admin/access/requests/{request_id}/reject")
async def reject(request_id: str, payload: DecisionIn,
                 user: AuthUser = Depends(require_admin)):
    from app.services import access_requests

    try:
        return await asyncio.to_thread(access_requests.decide, request_id, "reject",
                                       user.username, payload.note, None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
