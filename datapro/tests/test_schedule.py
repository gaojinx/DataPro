"""调度与迁移单测（不启动服务、不连数据库）。"""
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import HTTPException

from app.db.session import BASE_DIR
from app.services import schedule_registry as registry_mod
from app.services import schedule_runner
from app.services.auth import AuthUser


@pytest.fixture(autouse=True)
def _clean_signatures():
    schedule_runner._SIGNATURES.clear()
    yield
    schedule_runner._SIGNATURES.clear()


def _task(**kw):
    base = {"id": "t1", "freq": "daily", "time": "09:00", "weekday": 1, "enabled": True,
            "question": "查昨天的支付订单数"}
    base.update(kw)
    return base


def test_cron_daily_and_weekly():
    daily = schedule_runner._cron(_task())
    assert "hour='9'" in str(daily) and "minute='0'" in str(daily)
    weekly = schedule_runner._cron(_task(freq="weekly", weekday=7, time="08:30"))
    assert "day_of_week='6'" in str(weekly)          # 周日：1=周一 → 0=周一，7→6
    assert schedule_runner._cron(_task(time="24:00")) is None
    assert schedule_runner._cron(_task(time="abc")) is None


def test_sync_jobs_add_update_remove(monkeypatch):
    tasks = [_task()]
    monkeypatch.setattr(schedule_runner.schedule_registry, "list", lambda: tasks)
    sched = AsyncIOScheduler()
    schedule_runner.sync_jobs(sched)
    assert sched.get_job("sched:t1") is not None

    # 问题变了 → 签名变化，重建作业
    tasks[0] = _task(question="查本周的支付订单数")
    schedule_runner.sync_jobs(sched)
    assert schedule_runner._SIGNATURES["sched:t1"] == schedule_runner._signature(tasks[0])

    # 停用 → 移除作业与签名
    tasks[0] = _task(enabled=False)
    schedule_runner.sync_jobs(sched)
    assert sched.get_job("sched:t1") is None
    assert "sched:t1" not in schedule_runner._SIGNATURES


def test_alembic_baseline_present():
    assert (BASE_DIR / "alembic.ini").exists()
    assert (BASE_DIR / "alembic" / "versions" / "0001_baseline.py").exists()


def test_list_filters_by_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "DATA_FILE", tmp_path / "schedules.json")
    reg = registry_mod.ScheduleRegistry()
    reg.add({"name": "a", "question": "查订单"}, owner="alice")
    reg.add({"name": "b", "question": "查销售"}, owner="bob")
    assert [t["name"] for t in reg.list(owner="alice")] == ["a"]
    assert len(reg.list(owner="alice", all_users=True)) == 2
    assert len(reg.list()) == 2                       # 调度循环需要全量


def test_delete_by_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "DATA_FILE", tmp_path / "schedules.json")
    reg = registry_mod.ScheduleRegistry()
    reg.add({"name": "a", "question": "查订单"}, owner="alice")
    reg.add({"name": "b", "question": "查销售"}, owner="bob")
    assert reg.delete_by_owner("alice") == 1
    assert [t["owner"] for t in reg.list()] == ["bob"]
    assert reg.delete_by_owner("") == 0
    assert reg.delete_by_owner("nobody") == 0


async def test_delete_user_clears_schedules(monkeypatch):
    """删除账号时同步清理其定时任务，避免遗留任务按已删账号身份继续跑。"""
    from app.api import routes_admin
    from app.services import token_store as token_mod
    from app.services.db_access import db_access

    calls = {}

    def fake_delete_by_owner(owner):
        calls["owner"] = owner
        return 2

    monkeypatch.setattr(registry_mod.schedule_registry, "delete_by_owner", fake_delete_by_owner)
    monkeypatch.setattr(db_access, "set_tables", lambda *a, **k: None)
    monkeypatch.setattr(routes_admin, "_delete_user", lambda uid, actor: "bob")

    async def fake_bump(_username):
        return 1
    monkeypatch.setattr(token_mod.token_store, "bump_epoch", fake_bump)

    admin = AuthUser(id=1, username="admin", display_name="管理员", role="admin")
    result = await routes_admin.delete_user(2, user=admin)
    assert result["deleted"] == 2
    assert calls["owner"] == "bob"


def test_route_owner_guard(monkeypatch):
    from app.api import routes_schedules as routes

    monkeypatch.setattr(routes.schedule_registry, "get",
                        lambda tid: {"id": tid, "owner": "bob"})
    alice = AuthUser(id=2, username="alice", display_name="A", role="user",
                     permissions=["schedules.manage"])
    with pytest.raises(HTTPException) as exc:
        routes._task_or_404("t1", alice)
    assert exc.value.status_code == 404

    admin = AuthUser(id=1, username="admin", display_name="A", role="admin")
    assert routes._task_or_404("t1", admin)["id"] == "t1"
