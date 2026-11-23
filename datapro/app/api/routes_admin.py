"""管理端：元数据维护、评测触发、LLM API 管理（功能清单 B16）。

RBAC：管理接口要求请求头 X-User-Role: admin（正式认证体系见 B12 JWT + RBAC）。
"""
import asyncio

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(role: str | None) -> None:
    if role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def admin_header() -> str | None:
    """X-User-Role 请求头（正式认证体系见 B12 JWT + RBAC）。"""
    return Header(default=None, alias="X-User-Role")


@router.get("/metadata/tables")
async def list_tables(role: str | None = admin_header()):
    require_admin(role)
    from app.services.recall import SEED_TABLES
    return SEED_TABLES


@router.get("/metadata/calibers")
async def list_calibers(role: str | None = admin_header()):
    require_admin(role)
    from app.services.recall import SEED_CALIBERS
    return SEED_CALIBERS


@router.post("/eval/run")
async def run_eval(dataset: str = "eval_v1_seed", role: str | None = admin_header()):
    require_admin(role)
    from eval.runner import run_evaluation
    return await run_evaluation(dataset)


# ---------- LLM API 管理（B16） ----------

class LLMApiIn(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True


@router.get("/llm-apis")
async def list_llm_apis(role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    return llm_registry.list()


@router.post("/llm-apis")
async def add_llm_api(body: LLMApiIn, role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    return llm_registry.add(body.model_dump())


@router.put("/llm-apis/{api_id}")
async def update_llm_api(api_id: str, body: dict, role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    result = llm_registry.update(api_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="API 不存在")
    return result


@router.delete("/llm-apis/{api_id}")
async def delete_llm_api(api_id: str, role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    if not llm_registry.delete(api_id):
        raise HTTPException(status_code=404, detail="API 不存在")
    return {"deleted": api_id}


@router.post("/llm-apis/{api_id}/default")
async def set_default_llm_api(api_id: str, role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    if not llm_registry.set_default(api_id):
        raise HTTPException(status_code=404, detail="API 不存在")
    return {"default": api_id}


@router.post("/llm-apis/{api_id}/test")
async def test_llm_api(api_id: str, role: str | None = admin_header()):
    require_admin(role)
    from app.services.llm_registry import llm_registry
    return await llm_registry.test(api_id)


# ---------- 普通用户只读状态 ----------

@router.get("/llm/status")
async def llm_status():
    from app.services.llm_registry import llm_registry
    return llm_registry.status()


@router.post("/sessions/sweep")
async def sweep_sessions(role: str | None = Header(None, alias="X-User-Role"),
                         ttl_hours: int | None = None):
    """S07：手动触发会话账号与临时表回收（不传 ttl_hours 用配置值）。"""
    require_admin(role)
    from app.services.session_janitor import sweep
    return await asyncio.to_thread(sweep, ttl_hours)
