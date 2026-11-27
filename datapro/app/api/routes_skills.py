"""技能包（Skill）：全员可读启用项，管理员增删改。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, get_current_user, require_permission
from app.services.skill_registry import skill_registry

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillIn(BaseModel):
    name: str
    description: str = ""
    question: str
    answer: str = ""          # 非空则为预设回复技能：命中后直接回复，不走取数
    enabled: bool = True


@router.get("")
async def list_skills(all: int = 0, user: AuthUser = Depends(get_current_user)):
    # 普通用户只能看启用项；有技能维护权限可看全部
    show_all = bool(all) and user.can("skills.manage")
    return skill_registry.list(only_enabled=not show_all)


def _check_answer_permission(body: dict, user: AuthUser, existing: dict | None = None) -> None:
    """预设回复绕过取数链路影响所有命中用户，仅管理员可配置。

    不仅拦新增 answer：修改已有预设回复技能的 question/enabled 同样能改变
    「谁命中、是否命中」，属于间接操控，必须一并拒绝（防止 PUT 只改 question 绕过）。
    """
    if user.role == "admin":
        return
    has_answer = bool((body.get("answer") or "").strip()) or bool(
        (existing or {}).get("answer"))
    if has_answer:
        raise HTTPException(status_code=403, detail="仅管理员可配置或修改预设回复技能")


@router.post("")
async def add_skill(body: SkillIn, user: AuthUser = Depends(require_permission("skills.manage"))):
    data = body.model_dump()
    _check_answer_permission(data, user)
    return skill_registry.add(data)


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: dict,
                       user: AuthUser = Depends(require_permission("skills.manage"))):
    existing = next((s for s in skill_registry.list() if s["id"] == skill_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    # 既拦「新增 answer」，也拦「修改已有预设回复技能」（含只改 question 的绕过）
    _check_answer_permission(body, user, existing)
    result = skill_registry.update(skill_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    return result


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, user: AuthUser = Depends(require_permission("skills.manage"))):
    existing = next((s for s in skill_registry.list() if s["id"] == skill_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    # 删除预设回复技能同样改变全局命中行为，仅管理员可操作
    if user.role != "admin" and (existing.get("answer") or "").strip():
        raise HTTPException(status_code=403, detail="仅管理员可删除预设回复技能")
    if not skill_registry.delete(skill_id):
        raise HTTPException(status_code=404, detail="技能不存在")
    return {"deleted": skill_id}
