"""权限矩阵单测（B12）：角色默认、用户级覆盖、非法值过滤、细粒度依赖。"""
import pytest
from fastapi import HTTPException

from app.services.auth import AuthUser, require_permission
from app.services.permissions import ALL_CODES, DEFAULT_USER_CODES, PermissionMatrix


@pytest.fixture()
def matrix(tmp_path):
    return PermissionMatrix(tmp_path / "roles.json")


def test_default_matrix_and_resolve(matrix):
    assert matrix.role_codes("user") == DEFAULT_USER_CODES
    assert matrix.role_codes("admin") == ALL_CODES
    assert matrix.resolve("user", None) == DEFAULT_USER_CODES
    # 管理类权限不可下放给普通用户（提权防线）
    assert matrix.resolve("user", '["users.manage","chat.use"]') == ["chat.use"]
    assert matrix.resolve("user", "[]") == []           # 显式无权限
    assert matrix.resolve("user", "not-json") == DEFAULT_USER_CODES
    assert matrix.resolve("admin", "[]") == ALL_CODES   # 管理员恒为全量


def test_default_user_can_use_content_ops():
    # 普通用户默认可使用技能、定时任务与巡检；系统管理类权限仍不含
    assert {"skills.manage", "schedules.manage", "inspections.manage"} <= set(DEFAULT_USER_CODES)
    assert not {"users.manage", "llm.manage", "eval.run", "sessions.sweep"} & set(DEFAULT_USER_CODES)


def test_set_role_codes_persist(matrix, tmp_path):
    matrix.set_role_codes("user", ["chat.use", "bogus", "chat.use"])
    assert matrix.role_codes("user") == ["chat.use"]
    reloaded = PermissionMatrix(tmp_path / "roles.json")
    assert reloaded.role_codes("user") == ["chat.use"]
    with pytest.raises(ValueError):
        matrix.set_role_codes("admin", ALL_CODES)


def test_normalize():
    assert PermissionMatrix.normalize(["chat.use", "x", "chat.use"]) == ["chat.use"]
    assert PermissionMatrix.normalize([]) == []
    assert PermissionMatrix.normalize(None) is None
    # 用户级覆盖不能写入管理类权限
    assert PermissionMatrix.normalize(["chat.use", "llm.manage", "eval.run"]) == ["chat.use"]


def test_set_role_codes_rejects_admin_only(matrix):
    """角色默认矩阵也无法给普通角色配置管理类权限。"""
    codes = matrix.set_role_codes("user", ["chat.use", "users.manage"])
    assert codes == ["chat.use"]
    assert matrix.role_codes("user") == ["chat.use"]


async def test_require_permission():
    dep = require_permission("skills.manage")
    admin = AuthUser(id=1, username="a", display_name="A", role="admin")
    assert (await dep(admin)).username == "a"
    allowed = AuthUser(id=3, username="v", display_name="V", role="user",
                       permissions=["skills.manage"])
    assert (await dep(allowed)).username == "v"
    normal = AuthUser(id=2, username="u", display_name="U", role="user",
                      permissions=["chat.use"])
    with pytest.raises(HTTPException) as exc:
        await dep(normal)
    assert exc.value.status_code == 403