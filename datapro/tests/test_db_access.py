"""用户级数据表权限（A02/S1）单测：存储、校验器拦截、状态机 E003、管理接口。"""
from app.api import routes_admin
from app.core.statemachine import PipelineContext
from app.services.auth import AuthUser
from app.services.db_access import DbAccessRegistry, db_access
from app.services.validator import ValidatorService, validate_sql
from tests.test_statemachine import build


def test_registry_json_roundtrip(tmp_path):
    svc = DbAccessRegistry(path=tmp_path / "db_access.json", use_db=False)
    assert svc.tables_for("alice") is None          # 未配置 → 沿用全局白名单
    svc.set_tables("alice", ["DWD_Order_Detail_DI", "dwd_user_di"], "admin", 1)
    assert svc.tables_for("alice") == {"dwd_order_detail_di", "dwd_user_di"}
    assert svc.configured("alice")
    svc.set_tables("alice", None, "admin")          # 清除配置
    assert svc.tables_for("alice") is None


def test_validator_marks_forbidden_tables():
    sql = "SELECT order_id FROM dwd_order_detail_di LIMIT 10"
    result = validate_sql(sql, {"dwd_order_detail_di", "dwd_user_di"},
                          user_tables={"dwd_user_di"})
    assert not result.passed
    assert result.forbidden == ["dwd_order_detail_di"]

    ok = validate_sql(sql, {"dwd_order_detail_di"}, user_tables={"dwd_order_detail_di"})
    assert ok.passed and ok.forbidden == []

    # 未配置（None）不限制，保持既有行为
    assert validate_sql(sql, {"dwd_order_detail_di"}).forbidden == []


async def test_validator_service_passes_user_tables():
    result = await ValidatorService().validate(
        "SELECT 1 AS c FROM dwd_order_detail_di",
        ["dwd_order_detail_di", "dwd_user_di"], user_tables={"dwd_user_di"})
    assert result.forbidden == ["dwd_order_detail_di"]


def _configure(monkeypatch, tmp_path, username, tables):
    monkeypatch.setattr(db_access, "_path", tmp_path / "db_access.json")
    monkeypatch.setattr(db_access, "_use_db", False)
    monkeypatch.setattr(db_access, "_cache", {})
    db_access.set_tables(username, tables, "admin")


async def test_pipeline_rejects_unauthorized_table(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "t", ["dwd_user_di"])
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t")
    events = [e async for e in build().run(ctx)]
    rejects = [e for e in events if e["type"] == "reject" and e["reason_code"] == "E003"]
    assert rejects
    assert rejects[0]["can_request"] is True
    assert "dwd_order_detail_di" in rejects[0]["tables"]


async def test_pipeline_admin_not_limited(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "admin", ["dwd_user_di"])
    ctx = PipelineContext(question="查昨天的支付订单数", user_id="admin", user_role="admin")
    events = [e async for e in build().run(ctx)]
    assert not any(e["type"] == "reject" and e["reason_code"] == "E003" for e in events)


async def test_table_access_routes(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "bob", None)
    monkeypatch.setattr(routes_admin, "_user_brief",
                        lambda uid: {"id": uid, "username": "bob", "role": "user"})

    got = await routes_admin.get_table_access(2, _=None)
    assert got["inherit"] is True and got["tables"] == []

    actor = AuthUser(id=1, username="admin", display_name="管理员", role="admin")
    body = routes_admin.TableAccessIn(inherit=False, tables=["dwd_order_detail_di"])
    saved = await routes_admin.set_table_access(2, body, actor=actor)
    assert saved["inherit"] is False
    assert saved["tables"] == ["dwd_order_detail_di"]

    got2 = await routes_admin.get_table_access(2, _=None)
    assert got2["inherit"] is False and got2["tables"] == ["dwd_order_detail_di"]

    cleared = await routes_admin.set_table_access(
        2, routes_admin.TableAccessIn(inherit=True), actor=actor)
    assert cleared["inherit"] is True


async def test_delete_user_clears_table_access(tmp_path, monkeypatch):
    """删除用户时同步清理 db_access，避免同名账号重建后继承旧授权。"""
    from app.services import token_store as token_mod

    _configure(monkeypatch, tmp_path, "bob", ["dwd_user_di"])
    monkeypatch.setattr(routes_admin, "_delete_user", lambda uid, actor: "bob")

    async def fake_bump(_username):
        return 1
    monkeypatch.setattr(token_mod.token_store, "bump_epoch", fake_bump)

    admin = AuthUser(id=1, username="admin", display_name="管理员", role="admin")
    result = await routes_admin.delete_user(2, user=admin)
    assert result["deleted"] == 2
    assert db_access.tables_for("bob", refresh=True) is None


async def test_export_respects_current_table_access(tmp_path, monkeypatch):
    """导出重跑同样受用户表权限约束（申请后收权也应被拦截）。"""
    from app.services import export_registry as exp_mod
    from app.services import executor as executor_mod
    from app.services.export_registry import ExportRegistry

    monkeypatch.setattr(exp_mod, "INDEX_PATH", tmp_path / "exports.json")
    monkeypatch.setattr(exp_mod, "EXPORT_DIR", tmp_path / "files")
    svc = ExportRegistry(use_db=False)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di")
    svc.decide(rec["id"], "approve", "admin")

    _configure(monkeypatch, tmp_path, "alice", ["dwd_user_di"])

    called = {}

    async def fake_fetch_all(*args, **kwargs):
        called["yes"] = True
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    monkeypatch.setattr(executor_mod.executor_service, "fetch_all", fake_fetch_all)
    updated = await svc.run_export(rec["id"])
    assert updated["status"] == "failed" and "无权访问" in updated["error"]
    assert "yes" not in called

