"""权限矩阵（B12）：权限点定义、角色默认矩阵与用户级覆盖。

- 权限点固定枚举，避免拼写错误导致越权或误拦
- 管理员固定全权（防止把自己锁死）；普通用户默认只可对话与文件操作
- 角色默认矩阵存业务库 `role_permission`（0014），库不可用时降级
  `data/role_permissions.json`；首次读到空表时自动从历史文件种子化
- 用户级覆盖存 `user_account.permissions`（JSON 数组；NULL 表示继承角色默认）
"""
import json
import logging
import threading
import time
from pathlib import Path

from app.services.shared_json import SharedJson

logger = logging.getLogger("datapro.permissions")

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "role_permissions.json"
_DB_RETRY_SECONDS = 30.0
_CACHE_TTL = 5.0            # 授权依据 5 秒内复用，写操作立即失效

PERMISSIONS = [
    {"code": "chat.use", "label": "使用对话取数", "group": "对话"},
    {"code": "files.upload", "label": "上传文件", "group": "文件"},
    {"code": "files.delete", "label": "删除文件", "group": "文件"},
    {"code": "skills.manage", "label": "维护技能", "group": "内容运营"},
    {"code": "schedules.manage", "label": "维护定时任务", "group": "内容运营"},
    {"code": "knowledge.manage", "label": "维护知识库", "group": "内容运营"},
    {"code": "inspections.manage", "label": "巡检与规则", "group": "数据运维"},
    {"code": "users.manage", "label": "管理用户", "group": "系统管理"},
    {"code": "llm.manage", "label": "管理模型 API", "group": "系统管理"},
    {"code": "eval.run", "label": "运行评测", "group": "系统管理"},
    {"code": "sessions.sweep", "label": "回收会话资源", "group": "系统管理"},
]

ALL_CODES = [p["code"] for p in PERMISSIONS]
# 管理类权限只能由 admin 角色持有：用户级覆盖与角色默认矩阵都不得授予。
# 这些能力影响全局/跨用户，曾可被普通用户通过 PATCH /users/{id} 拿到（如 llm.manage → SSRF）。
# knowledge.manage 为管理员专属（知识库路由声明 + 前端侧栏硬编码 admin），
# 若允许下放，普通用户即可绕过 UI 直接调 /api/v1/knowledge 读写全局口径知识。
ADMIN_ONLY_CODES = frozenset({"users.manage", "llm.manage", "eval.run",
                              "sessions.sweep", "knowledge.manage"})
# 普通用户默认可使用对话、文件、技能、定时任务与巡检（管理类权限仍仅管理员）
DEFAULT_USER_CODES = ["chat.use", "files.upload", "files.delete",
                      "skills.manage", "schedules.manage", "inspections.manage"]
EDITABLE_ROLES = ("user",)


def non_admin_codes(codes: list[str] | None) -> list[str]:
    """过滤掉仅限管理员的权限点；返回去重后的普通用户可持有权限。"""
    if not codes:
        return []
    return [c for c in dict.fromkeys(codes) if c in ALL_CODES and c not in ADMIN_ONLY_CODES]


class PermissionMatrix:
    def __init__(self, path: Path | None = None, use_db: bool | None = None):
        self._path = path or DATA_FILE
        # 显式传入 path 视为测试/单文件场景，不访问业务库
        self._use_db = (path is None) if use_db is None else use_db
        self._lock = threading.RLock()
        self._db_retry_at = 0.0
        self._cache: dict[str, list[str]] | None = None
        self._cache_at = 0.0

    # ---------- 存储后端 ----------
    def _store(self) -> SharedJson:
        return SharedJson(self._path, default={"roles": {}})

    def _db_ready(self) -> bool:
        return self._use_db and time.monotonic() >= self._db_retry_at

    def _db_failed(self, exc: Exception) -> None:
        self._db_retry_at = time.monotonic() + _DB_RETRY_SECONDS
        logger.warning("角色权限表不可用，降级文件（30 秒后重试）：%s", exc)

    def _from_file(self) -> dict[str, list[str]]:
        roles: dict[str, list[str]] = {}
        try:
            if self._path.exists():
                for role, codes in (self._store().snapshot().get("roles") or {}).items():
                    if role != "admin":
                        roles[role] = non_admin_codes(codes)
        except Exception as exc:  # noqa: BLE001 —— 配置损坏时回退默认矩阵
            logger.warning("读取角色权限矩阵文件失败：%s", exc)
        return roles

    def _read_db(self) -> dict[str, list[str]] | None:
        if not self._db_ready():
            return None
        try:
            from app.db.models import RolePermission
            from app.db.session import SessionLocal

            with SessionLocal() as db:
                rows = db.query(RolePermission).all()
                return {r.role: non_admin_codes(json.loads(r.permissions or "[]")) for r in rows}
        except Exception as exc:  # noqa: BLE001 —— 库不可用时读文件
            self._db_failed(exc)
            return None

    def _seed_db(self, roles: dict[str, list[str]]) -> None:
        try:
            from app.db.models import RolePermission
            from app.db.session import SessionLocal

            with SessionLocal() as db:
                if db.query(RolePermission).count():
                    return
                payload = roles or {"user": list(DEFAULT_USER_CODES)}
                for role, codes in payload.items():
                    if role == "admin":
                        continue
                    db.add(RolePermission(role=role,
                                          permissions=json.dumps(non_admin_codes(codes)),
                                          updated_by="bootstrap"))
                db.commit()
        except Exception as exc:  # noqa: BLE001 —— 种子失败仍按默认值工作
            self._db_failed(exc)

    # ---------- 读取 ----------
    def _load(self) -> dict[str, list[str]]:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cache_at < _CACHE_TTL:
                return self._cache

        roles: dict[str, list[str]] = {"user": list(DEFAULT_USER_CODES)}
        db_roles = self._read_db() if self._use_db else None
        if db_roles is not None:
            if not db_roles:
                self._seed_db(self._from_file())
                db_roles = self._read_db() or {}
            roles.update(db_roles or {})
        else:
            roles.update(self._from_file())

        with self._lock:
            self._cache = roles
            self._cache_at = time.monotonic()
        return roles

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_at = 0.0

    def role_codes(self, role: str) -> list[str]:
        if role == "admin":
            return list(ALL_CODES)
        return list(self._load().get(role, []))

    def roles(self) -> dict[str, list[str]]:
        """矩阵展示用：管理员全量 + 可编辑角色的当前默认。"""
        data = {"admin": list(ALL_CODES)}
        for role in EDITABLE_ROLES:
            data[role] = self.role_codes(role)
        return data

    def resolve(self, role: str, raw: str | None) -> list[str]:
        """计算最终生效权限：管理员全量 → 用户覆盖 → 角色默认。"""
        if role == "admin":
            return list(ALL_CODES)
        if raw is None or raw == "":
            return self.role_codes(role)
        try:
            codes = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (ValueError, TypeError):
            return self.role_codes(role)
        return non_admin_codes(codes)

    # ---------- 写入 ----------
    def set_role_codes(self, role: str, codes: list[str], updated_by: str = "") -> list[str]:
        if role == "admin":
            raise ValueError("管理员固定全权，不能修改")
        if role not in EDITABLE_ROLES:
            raise ValueError("未知角色")
        clean = non_admin_codes(codes)

        if self._db_ready():
            try:
                from app.db.models import RolePermission
                from app.db.session import SessionLocal

                with SessionLocal() as db:
                    row = db.get(RolePermission, role) or RolePermission(role=role)
                    row.permissions = json.dumps(clean, ensure_ascii=False)
                    row.updated_by = updated_by or ""
                    db.add(row)
                    db.commit()
                self._invalidate()
                return clean
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级文件
                self._db_failed(exc)

        with self._store().edit() as data:
            data.setdefault("roles", {})[role] = clean
        self._invalidate()
        return clean

    @staticmethod
    def normalize(codes: list[str] | None) -> list[str] | None:
        """过滤非法权限码；None 表示继承角色默认，空数组表示显式无权限。"""
        if codes is None:
            return None
        return non_admin_codes(codes)


matrix = PermissionMatrix()
