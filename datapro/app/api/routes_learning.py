"""自动学习管理接口（A11）：技能规范与长期记忆的查看、启停、删除。

- 普通用户只看/管理自己的数据；管理员可 all=1 查看全部（便于排查）
- 权限沿用 `skills.manage`（普通用户默认拥有）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, require_permission

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


class EnabledIn(BaseModel):
    enabled: bool = True


def _all_flag(all: int, user: AuthUser) -> bool:
    return bool(all) and user.role == "admin"


@router.get("/skills")
async def list_learned_skills(all: int = 0,
                              user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.learned_skills import learned_skills
    return learned_skills.list(user.username, all_users=_all_flag(all, user))


@router.put("/skills/{skill_id}")
async def update_learned_skill(skill_id: str, body: EnabledIn,
                               user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.learned_skills import learned_skills
    item = learned_skills.update(skill_id, {"enabled": body.enabled},
                                 username=user.username, is_admin=user.role == "admin")
    if not item:
        raise HTTPException(status_code=404, detail="技能规范不存在")
    return item


@router.delete("/skills/{skill_id}")
async def delete_learned_skill(skill_id: str,
                               user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.learned_skills import learned_skills
    if not learned_skills.delete(skill_id, username=user.username, is_admin=user.role == "admin"):
        raise HTTPException(status_code=404, detail="技能规范不存在")
    return {"deleted": skill_id}


@router.get("/memories")
async def list_memories(all: int = 0,
                        user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.memory_store import memory_store
    return memory_store.list(user.username, all_users=_all_flag(all, user))


@router.put("/memories/{mem_id}")
async def update_memory(mem_id: str, body: EnabledIn,
                        user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.memory_store import memory_store
    item = memory_store.update(mem_id, {"enabled": body.enabled},
                               username=user.username, is_admin=user.role == "admin")
    if not item:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return item


@router.delete("/memories/{mem_id}")
async def delete_memory(mem_id: str,
                        user: AuthUser = Depends(require_permission("skills.manage"))):
    from app.services.memory_store import memory_store
    if not memory_store.delete(mem_id, username=user.username, is_admin=user.role == "admin"):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": mem_id}
