"""API 越权回归测试（确保普通用户无法经 API 触达管理员能力）。

覆盖四道防线：
1. knowledge.manage 归入管理员专属，无法下放到普通用户
2. /admin/llm/status 要求登录
3. 标记码溯源按归属隔离，普通用户不能查他人问答
4. 预设回复技能的新增/修改/删除仅管理员可用（含只改 question 的绕过）
"""
import pytest
from fastapi import HTTPException

from app.services.auth import AuthUser


ADMIN = AuthUser(id=1, username="admin", display_name="管理员", role="admin",
                 permissions=[])
USER = AuthUser(id=2, username="analyst", display_name="分析师", role="user",
                permissions=["chat.use", "skills.manage"])


# ---------- 1. knowledge.manage 管理员专属 ----------

def test_knowledge_manage_is_admin_only(tmp_path):
    from app.services.permissions import ADMIN_ONLY_CODES, PermissionMatrix

    assert "knowledge.manage" in ADMIN_ONLY_CODES
    # 用临时文件隔离，避免测试改写真实角色矩阵
    matrix = PermissionMatrix(tmp_path / "roles.json")
    # 角色默认矩阵与用户级覆盖都无法给普通用户授予知识库管理权限
    assert "knowledge.manage" not in matrix.set_role_codes("user", ["knowledge.manage"])
    assert "knowledge.manage" not in matrix.resolve("user", '["knowledge.manage"]')
    assert "knowledge.manage" not in PermissionMatrix.normalize(["knowledge.manage"])
    # 管理员恒持有
    assert "knowledge.manage" in matrix.role_codes("admin")


# ---------- 2. llm/status 鉴权依赖 ----------

async def test_llm_status_requires_login():
    """端点声明了 get_current_user 依赖：匿名调用会被 FastAPI 拒绝。"""
    from app.api import routes_admin
    from app.services.auth import get_current_user

    route = next(r for r in routes_admin.router.routes if r.path.endswith("/llm/status"))
    defaults = route.dependant.call.__defaults__ or ()
    # fastapi.Depends 是返回依赖标记的函数对象，用属性判定而非 isinstance
    assert any(getattr(d, "dependency", None) is get_current_user for d in defaults)
    # 无参（匿名）调用不再可能：依赖注入层会先抛 401
    with pytest.raises(HTTPException):
        await get_current_user(None)


# ---------- 3. 标记码溯源归属隔离 ----------

class _FakeQuery:
    def __init__(self, username):
        self.id = 7
        self.username = username
        self.question = "查昨天的支付订单数"
        self.question_context = None
        self.final_answer = "123"
        self.generated_sql = "SELECT 1"
        self.session_id = "s-1"
        self.trace_id = "t-1"
        self.row_count = 1
        self.latency_ms = 10
        from datetime import datetime
        self.created_at = datetime(2026, 1, 1)


class _FakeDB:
    def __init__(self, rec):
        self.rec = rec

    def get(self, _model, _pk):
        return self.rec

    def close(self):
        pass


def _call_trace(rec, user, monkeypatch):
    from app.api import routes_trace

    monkeypatch.setattr(routes_trace, "SessionLocal", lambda: _FakeDB(rec))
    return routes_trace.get_trace("DP-0007", user)


def test_trace_owner_can_read_own(monkeypatch):
    result = _call_trace(_FakeQuery("analyst"), USER, monkeypatch)
    assert result["code"] == "DP-0007"
    assert result["question"] == "查昨天的支付订单数"


def test_trace_other_user_denied(monkeypatch):
    # 他人记录对普通用户表现为「不存在」，不泄露资源是否存在
    with pytest.raises(HTTPException) as exc:
        _call_trace(_FakeQuery("someone-else"), USER, monkeypatch)
    assert exc.value.status_code == 404


def test_trace_admin_can_read_any(monkeypatch):
    result = _call_trace(_FakeQuery("someone-else"), ADMIN, monkeypatch)
    assert result["code"] == "DP-0007"
    assert result["question"] == "查昨天的支付订单数"


# ---------- 4. 预设回复技能授权绕过 ----------

def test_add_preset_reply_by_normal_user_denied():
    from app.api.routes_skills import _check_answer_permission

    with pytest.raises(HTTPException) as exc:
        _check_answer_permission({"answer": "固定回复"}, USER)
    assert exc.value.status_code == 403


def test_modify_existing_preset_reply_by_normal_user_denied():
    """只改 question 也不行：仍会改变预设回复的命中条件。"""
    from app.api.routes_skills import _check_answer_permission

    with pytest.raises(HTTPException) as exc:
        _check_answer_permission({"question": "换个问法"}, USER,
                                 existing={"answer": "固定回复"})
    assert exc.value.status_code == 403


def test_normal_skill_edit_still_allowed():
    from app.api.routes_skills import _check_answer_permission

    # 非预设回复技能的常规编辑不受影响
    _check_answer_permission({"question": "新问法"}, USER,
                             existing={"answer": ""})


def test_admin_can_manage_preset_reply():
    from app.api.routes_skills import _check_answer_permission

    _check_answer_permission({"answer": "固定回复"}, ADMIN)
    _check_answer_permission({"question": "换个问法"}, ADMIN,
                             existing={"answer": "固定回复"})
