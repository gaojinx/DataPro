"""定时任务：全员可用（`schedules.manage`）。

普通用户只能查看/维护本人创建的任务，管理员可管理全部；调度循环见 schedule_runner。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, require_permission
from app.services.schedule_registry import schedule_registry
from app.services.schedule_runner import run_task

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str
    question: str = ""           # type=ingest 时可为空
    type: str = "question"       # question | ingest
    ingest_code: str = ""        # type=ingest 时指定数据源编码，如 xianzhi
    freq: str = "daily"          # daily | weekly
    time: str = "09:00"          # HH:MM
    weekday: int = 1             # 1=周一 … 7=周日（weekly 生效）
    enabled: bool = True


def _task_or_404(task_id: str, user: AuthUser) -> dict:
    """任务不存在或不属于当前用户时统一 404，避免泄露他人任务是否存在。"""
    task = schedule_registry.get(task_id)
    if not task or (user.role != "admin" and task.get("owner") != user.username):
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("")
async def list_schedules(user: AuthUser = Depends(require_permission("schedules.manage"))):
    return schedule_registry.list(owner=user.username, all_users=user.role == "admin")


@router.post("")
async def add_schedule(body: ScheduleIn, user: AuthUser = Depends(require_permission("schedules.manage"))):
    return schedule_registry.add(body.model_dump(), owner=user.username)


@router.put("/{task_id}")
async def update_schedule(task_id: str, body: dict,
                          user: AuthUser = Depends(require_permission("schedules.manage"))):
    _task_or_404(task_id, user)
    result = schedule_registry.update(task_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@router.delete("/{task_id}")
async def delete_schedule(task_id: str, user: AuthUser = Depends(require_permission("schedules.manage"))):
    _task_or_404(task_id, user)
    if not schedule_registry.delete(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"deleted": task_id}


@router.post("/{task_id}/run")
async def run_schedule(task_id: str, user: AuthUser = Depends(require_permission("schedules.manage"))):
    task = _task_or_404(task_id, user)
    return await run_task(task)


@router.get("/{task_id}/runs")
async def schedule_runs(task_id: str, user: AuthUser = Depends(require_permission("schedules.manage"))):
    return _task_or_404(task_id, user).get("runs", [])
