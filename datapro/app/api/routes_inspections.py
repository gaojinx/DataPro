"""巡检接口（A08/F16）：结果查询、规则维护、手动触发。

普通用户默认可查看与操作（`inspections.manage`），管理员可在权限矩阵中调整。
"""
import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.services.auth import AuthUser, require_permission

router = APIRouter(prefix="/api/v1/inspections", tags=["inspections"])


class RulesIn(BaseModel):
    rules: list[dict]


@router.get("")
async def list_results(_: AuthUser = Depends(require_permission("inspections.manage"))):
    from app.services.inspection import inspection_service
    return {"summary": inspection_service.summary(), **inspection_service.results()}


@router.get("/rules")
async def get_rules(_: AuthUser = Depends(require_permission("inspections.manage"))):
    from app.services.inspection import inspection_service
    return inspection_service.rules()


@router.put("/rules")
async def save_rules(payload: RulesIn,
                     _: AuthUser = Depends(require_permission("inspections.manage"))):
    from fastapi import HTTPException

    from app.services.inspection import inspection_service
    try:
        return inspection_service.save_rules(payload.rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run")
async def run_now(user: AuthUser = Depends(require_permission("inspections.manage"))):
    from app.services.inspection import inspection_service
    return await asyncio.to_thread(inspection_service.run_all, user.username)
