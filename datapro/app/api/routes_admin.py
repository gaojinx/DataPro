"""管理端：元数据维护、评测触发、LLM API 管理、用户管理（功能清单 B16/B12）。

RBAC：管理接口要求 Bearer Token 且角色为 admin（B12）。
"""
import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, get_current_user, hash_password, require_admin, require_permission

logger = logging.getLogger("datapro.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metadata/tables")
async def list_tables(_: AuthUser = Depends(require_admin)):
    from app.services.metadata_store import metadata_store
    return metadata_store.tables()


@router.get("/metadata/calibers")
async def list_calibers(_: AuthUser = Depends(require_admin)):
    from app.services.metadata_store import metadata_store
    return metadata_store.calibers()


@router.get("/calibers/{metric_id}/history")
async def caliber_history(metric_id: str, limit: int = 50,
                          _: AuthUser = Depends(require_admin)):
    """口径变更历史（P2）：按时间倒序返回该指标的历史快照。"""
    def _load() -> list[dict]:
        from app.db.models import MetricCaliberHistory
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            rows = (db.query(MetricCaliberHistory)
                    .filter(MetricCaliberHistory.metric_id == metric_id)
                    .order_by(MetricCaliberHistory.changed_at.desc(),
                              MetricCaliberHistory.id.desc())
                    .limit(min(max(limit, 1), 200)).all())
            return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]

    return await asyncio.to_thread(_load)


class RulingIn(BaseModel):
    metric_id: str
    note: str = ""


@router.get("/calibers/rulings")
async def list_rulings(_: AuthUser = Depends(require_admin)):
    from app.services.caliber_rulings import caliber_rulings
    return [{"term": term, **ruling} for term, ruling in caliber_rulings.read().items()]


@router.put("/calibers/rulings/{term}")
async def set_ruling(term: str, payload: RulingIn,
                     user: AuthUser = Depends(require_admin)):
    from app.services.caliber_rulings import caliber_rulings
    try:
        return caliber_rulings.set(term, payload.metric_id, payload.note, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/calibers/rulings/{term}")
async def delete_ruling(term: str, _: AuthUser = Depends(require_admin)):
    from app.services.caliber_rulings import caliber_rulings
    if not caliber_rulings.remove(term):
        raise HTTPException(status_code=404, detail="裁决不存在")
    return {"term": term, "deleted": True}


@router.get("/records")
async def list_records(username: str = "", days: int = 7, rating: str = "",
                       keyword: str = "", page: int = 1, page_size: int = 20,
                       outcome: str = "", error_code: str = "",
                       _: AuthUser = Depends(require_admin)):
    """管理后台对话记录：按用户/天数/反馈/结果分类/错误码/关键词筛选，时间倒序分页。"""
    from app.services import persist
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    items, total = await persist.list_records(username.strip(), days, rating,
                                              keyword.strip(), page, page_size,
                                              outcome.strip(), error_code.strip())
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/eval/run")
async def run_eval(dataset: str = "eval_v1_seed",
                   _: AuthUser = Depends(require_permission("eval.run"))):
    from eval.runner import run_evaluation
    return await run_evaluation(dataset)


# ---------- LLM API 管理（B16） ----------

class LLMApiIn(BaseModel):
    name: str
    base_url: str
    model: str
    api_key_ref: str = ""    # 密钥引用（env:/file:/kms://），不接受明文 Key
    enabled: bool = True


@router.get("/llm-apis")
async def list_llm_apis(_: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    return llm_registry.list()


@router.post("/llm-apis")
async def add_llm_api(body: LLMApiIn, _: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    try:
        return llm_registry.add(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/llm-apis/{api_id}")
async def update_llm_api(api_id: str, body: dict,
                         _: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    try:
        result = llm_registry.update(api_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="API 不存在")
    return result


@router.delete("/llm-apis/{api_id}")
async def delete_llm_api(api_id: str, _: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    if not llm_registry.delete(api_id):
        raise HTTPException(status_code=404, detail="API 不存在")
    return {"deleted": api_id}


@router.post("/llm-apis/{api_id}/default")
async def set_default_llm_api(api_id: str, _: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    if not llm_registry.set_default(api_id):
        raise HTTPException(status_code=404, detail="API 不存在")
    return {"default": api_id}


@router.post("/llm-apis/{api_id}/test")
async def test_llm_api(api_id: str, _: AuthUser = Depends(require_permission("llm.manage"))):
    from app.services.llm_registry import llm_registry
    return await llm_registry.test(api_id)


# ---------- 普通用户只读状态 ----------

@router.get("/llm/status")
async def llm_status(_: AuthUser = Depends(get_current_user)):
    """登录用户可读的模型状态：不暴露 Key，但也不对匿名开放。"""
    from app.services.llm_registry import llm_registry
    return llm_registry.status()


@router.post("/sessions/sweep")
async def sweep_sessions(_: AuthUser = Depends(require_permission("sessions.sweep")),
                         ttl_hours: int | None = None):
    """S07：手动触发会话账号与临时表回收（不传 ttl_hours 用配置值）。"""
    from app.services.session_janitor import sweep
    return await asyncio.to_thread(sweep, ttl_hours)


# ---------- 限定范围缓存（U-B03）：只读状态 + 人工清空 ----------

@router.get("/cache/stats")
async def cache_stats(_: AuthUser = Depends(require_admin)):
    """缓存状态：后端类型、命中率、各域 TTL。

    注意 counters 是**进程内**的：多实例部署时每副本各自计数，看板按 pod 汇总。
    """
    from app.services.query_cache import query_cache
    return query_cache.stats()


@router.post("/cache/clear")
async def cache_clear(scope: str = "", _: AuthUser = Depends(require_admin)):
    """清空缓存：口径/知识库大改后的人工兜底。

    日常不需要：键里带资料指纹，资料一变自然不命中；这里只处理「想立刻生效」的场景。
    """
    from app.services.query_cache import SCOPES, query_cache
    if scope and scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"未知缓存域：{scope}")
    removed = await query_cache.clear(scope or None)
    return {"removed": removed, "scope": scope or "all"}


# ---------- 用户管理（B12）：账号增删改查 + 令牌失效 ----------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
ROLES = {"admin", "user"}
STATUSES = {"active", "disabled"}


class UserIn(BaseModel):
    username: str
    display_name: str = ""
    password: str
    role: str = "user"


class UserPatch(BaseModel):
    display_name: str | None = None
    password: str | None = None
    role: str | None = None
    status: str | None = None
    permissions: list[str] | None = None     # 用户级权限覆盖；[] 表示显式无权限
    inherit_permissions: bool = False        # true 表示清除覆盖、继承角色默认


def _user_dict(u) -> dict:
    from app.services.permissions import matrix
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": u.role, "status": u.status,
        "permissions": json.loads(u.permissions) if u.permissions else None,
        "effective_permissions": matrix.resolve(u.role, u.permissions),
        "last_login_at": u.last_login_at.isoformat(timespec="seconds") if u.last_login_at else None,
        "created_at": u.created_at.isoformat(timespec="seconds") if u.created_at else None,
    }


def _list_users() -> list[dict]:
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return [_user_dict(u) for u in db.query(User).order_by(User.id).all()]


def _create_user(body: dict) -> dict:
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if db.query(User).filter(User.username == body["username"]).first():
            raise ValueError("账号已存在")
        user = User(username=body["username"],
                    display_name=body["display_name"] or body["username"],
                    role=body["role"], password_hash=hash_password(body["password"]))
        db.add(user)
        db.commit()
        db.refresh(user)
        return _user_dict(user)


def _update_user(user_id: int, patch: dict, actor: str) -> tuple[dict, bool]:
    """返回（用户信息, 是否需要使旧令牌失效）。"""
    from app.db.models import User
    from app.db.session import SessionLocal
    from app.services.permissions import matrix

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise LookupError("用户不存在")

        invalidate = False
        if patch.get("password"):
            user.password_hash = hash_password(patch["password"])
            invalidate = True
        if patch.get("display_name"):
            user.display_name = patch["display_name"]
        if patch.get("inherit_permissions"):
            if user.permissions is not None:
                user.permissions = None
                invalidate = True
        elif patch.get("permissions") is not None:
            from app.services.permissions import non_admin_codes
            # 管理类权限即使是管理员也不能下放给普通账号（USERNAME_RE 仅 admin/user 两角色）
            clean = non_admin_codes(patch["permissions"]) if user.role != "admin" else (
                matrix.normalize(patch["permissions"]) or [])
            user.permissions = json.dumps(clean, ensure_ascii=False)
            invalidate = True
        if patch.get("role") and patch["role"] != user.role:
            if user.username == actor:
                raise ValueError("不能修改当前登录账号的角色")
            if user.role == "admin" and _active_admins(db) <= 1:
                raise ValueError("至少保留一个启用的管理员")
            user.role = patch["role"]
            invalidate = True
        if patch.get("status") and patch["status"] != user.status:
            if user.username == actor:
                raise ValueError("不能禁用当前登录账号")
            if user.role == "admin" and _active_admins(db) <= 1:
                raise ValueError("至少保留一个启用的管理员")
            user.status = patch["status"]
            invalidate = True
        # 令牌版本与权限变更在同一事务递增：消除「先提交权限、后 bump」的旧令牌可用窗口
        if invalidate:
            user.token_version = int(user.token_version or 0) + 1
        db.commit()
        db.refresh(user)
        return _user_dict(user), invalidate


def _delete_user(user_id: int, actor: str) -> str:
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise LookupError("用户不存在")
        if user.username == actor:
            raise ValueError("不能删除当前登录账号")
        if user.role == "admin" and _active_admins(db) <= 1:
            raise ValueError("至少保留一个启用的管理员")
        username = user.username
        db.delete(user)
        db.commit()
        return username


def _active_admins(db) -> int:
    from app.db.models import User
    return (db.query(User).filter(User.role == "admin", User.status == "active").count())


def _validate_user(body: UserIn) -> dict:
    if not USERNAME_RE.match(body.username):
        raise ValueError("账号需为 2-32 位字母、数字、下划线、点或短横线")
    if len(body.password) < 6:
        raise ValueError("密码至少 6 位")
    if body.role not in ROLES:
        raise ValueError("角色只能是 admin 或 user")
    return body.model_dump()


@router.get("/users")
async def list_users(_: AuthUser = Depends(require_permission("users.manage"))):
    return {"users": await asyncio.to_thread(_list_users)}


@router.post("/users")
async def create_user(body: UserIn, _: AuthUser = Depends(require_permission("users.manage"))):
    try:
        data = _validate_user(body)
        return await asyncio.to_thread(_create_user, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserPatch,
                      user: AuthUser = Depends(require_permission("users.manage"))):
    patch = body.model_dump(exclude_none=True)
    if patch.get("role") and patch["role"] not in ROLES:
        raise HTTPException(status_code=400, detail="角色只能是 admin 或 user")
    if patch.get("status") and patch["status"] not in STATUSES:
        raise HTTPException(status_code=400, detail="状态只能是 active 或 disabled")
    if "password" in patch and len(patch["password"]) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    try:
        result, invalidate = await asyncio.to_thread(_update_user, user_id, patch, user.username)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if invalidate:
        # 版本已在 _update_user 同一事务内递增，这里只清缓存让变更立即生效
        from app.services import auth_state
        await asyncio.to_thread(auth_state.invalidate, result["username"])
    return result


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: AuthUser = Depends(require_permission("users.manage"))):
    try:
        username = await asyncio.to_thread(_delete_user, user_id, user.username)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # A02：同步清理该用户的数据表权限，避免同名账号重建时继承旧授权（失败不影响删除）
    from app.services.db_access import db_access
    try:
        await asyncio.to_thread(db_access.set_tables, username, None, user.username)
    except Exception as exc:  # noqa: BLE001 —— 清理失败只记日志
        logger.warning("清理用户 %s 的数据表权限失败：%s", username, exc)
    # 同步清理该用户创建的定时任务（任务按 owner 身份执行，账号删除后无意义）
    from app.services.schedule_registry import schedule_registry
    try:
        removed = await asyncio.to_thread(schedule_registry.delete_by_owner, username)
        if removed:
            logger.info("删除用户 %s 时清理定时任务 %s 个", username, removed)
    except Exception as exc:  # noqa: BLE001 —— 清理失败只记日志
        logger.warning("清理用户 %s 的定时任务失败：%s", username, exc)
    # 同步清理自动学习数据（A11）：长期记忆与技能规范按用户隔离，账号删除后一并移除
    from app.services.learned_skills import learned_skills
    from app.services.memory_store import memory_store
    try:
        await asyncio.to_thread(memory_store.clear_user, username)
        await asyncio.to_thread(learned_skills.clear_user, username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理用户 %s 的学习数据失败：%s", username, exc)
    return {"deleted": user_id}


# ---------- 用户级数据表权限（A02 / S1） ----------

class TableAccessIn(BaseModel):
    inherit: bool = True        # true=沿用全局表白名单；false=只允许 tables 列出的表
    tables: list[str] = []


def _user_brief(user_id: int) -> dict:
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise LookupError("用户不存在")
        return {"id": user.id, "username": user.username, "role": user.role}


@router.get("/users/{user_id}/table-access")
async def get_table_access(user_id: int,
                           _: AuthUser = Depends(require_permission("users.manage"))):
    from app.services.db_access import db_access
    try:
        user = await asyncio.to_thread(_user_brief, user_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tables = await asyncio.to_thread(db_access.tables_for, user["username"], True)
    return {"user_id": user_id, "username": user["username"], "role": user["role"],
            "inherit": tables is None, "tables": sorted(tables or [])}


@router.put("/users/{user_id}/table-access")
async def set_table_access(user_id: int, body: TableAccessIn,
                           actor: AuthUser = Depends(require_permission("users.manage"))):
    from app.services.db_access import db_access
    try:
        user = await asyncio.to_thread(_user_brief, user_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tables = None if body.inherit else body.tables
    saved = await asyncio.to_thread(db_access.set_tables, user["username"], tables,
                                    actor.username, user["id"])
    return {"user_id": user_id, "username": user["username"],
            "inherit": saved is None, "tables": saved or []}


# ---------- 权限矩阵（B12）：角色默认 + 用户覆盖 ----------

class RolePermissionsIn(BaseModel):
    permissions: list[str] = []


def _usernames_by_role(role: str) -> list[str]:
    from app.db.models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return [u.username for u in db.query(User).filter(User.role == role).all()]


@router.get("/permissions")
async def get_permissions(_: AuthUser = Depends(require_admin)):
    from app.services.permissions import PERMISSIONS, matrix

    def _meta() -> list[dict]:
        from app.db.models import RolePermission
        from app.db.session import SessionLocal
        try:
            with SessionLocal() as db:
                return [{"role": r.role, "updated_by": r.updated_by, "updated_at": r.updated_at}
                        for r in db.query(RolePermission).all()]
        except Exception as exc:  # noqa: BLE001 —— 元信息缺失不影响矩阵展示
            logger.warning("读取角色矩阵元信息失败：%s", exc)
            return []

    role_meta = await asyncio.to_thread(_meta)
    return {"permissions": PERMISSIONS, "roles": matrix.roles(), "role_meta": role_meta}


@router.get("/config-docs")
async def list_config_docs(key: str = "", _: AuthUser = Depends(require_admin)):
    """配置文档台账（0016）：哪些配置已入库、版本、最后修改人与时间。

    带 key 时返回该文档的完整 payload（管理员排障用：确认库里到底是什么）。
    """
    def _load() -> dict:
        import json as _json

        from app.db.models import ConfigDoc
        from app.db.session import SessionLocal
        from app.services.shared_json import DATA_DIR

        with SessionLocal() as db:
            query = db.query(ConfigDoc)
            if key:
                row = query.filter(ConfigDoc.doc_key == key).one_or_none()
                if row is None:
                    return {"items": [], "detail": None}
                return {"items": [], "detail": {
                    "doc_key": row.doc_key, "version": row.version,
                    "updated_by": row.updated_by, "updated_at": row.updated_at,
                    "payload": _json.loads(row.payload) if row.payload else None}}
            rows = query.order_by(ConfigDoc.doc_key).all()
        return {
            "root": str(DATA_DIR),
            "items": [{"doc_key": r.doc_key, "version": r.version,
                       "updated_by": r.updated_by, "updated_at": r.updated_at,
                       "chars": len(r.payload or "")} for r in rows],
            "detail": None,
        }

    return await asyncio.to_thread(_load)


@router.put("/permissions/roles/{role}")
async def set_role_permissions(role: str, body: RolePermissionsIn,
                               user: AuthUser = Depends(require_admin)):
    from app.services.permissions import matrix
    from app.services.token_store import token_store
    try:
        codes = matrix.set_role_codes(role, body.permissions, updated_by=user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 该角色默认权限变化：让继承默认的账号旧令牌失效，重新登录后拿到新权限快照
    for username in await asyncio.to_thread(_usernames_by_role, role):
        await token_store.bump_epoch(username)
    return {"role": role, "permissions": codes}
