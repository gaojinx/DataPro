"""数据表权限自助申请（U-B05）：有效期过滤、审批单状态机、增量授权。"""
from datetime import datetime, timedelta

import pytest

from app.services.access_requests import MAX_DURATION_DAYS, _clamp_days
from app.services.db_access import DbAccessRegistry, active_tables


def test_active_tables_filters_expired_only():
    now = datetime(2026, 11, 26, 12, 0, 0)
    rows = [
        ("orders", None),                                   # 永久
        ("users", now + timedelta(days=1)),                 # 未过期
        ("refunds", now - timedelta(seconds=1)),            # 刚过期
        ("campaign", now),                                  # 边界：等于当前时间即过期
    ]
    assert active_tables(rows, now) == {"orders", "users"}


def test_active_tables_all_expired_is_empty_not_none():
    """全部过期要返回空集合：调用方据此判定「已配置但无权限」，不能回退全局白名单。"""
    now = datetime(2026, 11, 26)
    assert active_tables([("orders", now - timedelta(days=1))], now) == set()


def test_duration_clamped():
    assert _clamp_days(0) == 1
    assert _clamp_days(-5) == 1
    assert _clamp_days(30) == 30
    assert _clamp_days(9999) == MAX_DURATION_DAYS
    assert _clamp_days(None) == 30


def test_resolve_table_uses_metadata_schema(monkeypatch):
    """元数据字典用 schema 存库名；申请落库不能恒写 dwd_shop。"""
    from app.services import access_requests
    from app.services import metadata_store as ms_module

    class _Store:
        @staticmethod
        def tables():
            return [{"table_name": "dwd_order_detail_di", "schema": "ods_shop"}]

    monkeypatch.setattr(ms_module, "metadata_store", _Store())
    assert access_requests._resolve_table(" DWD_ORDER_DETAIL_DI ") == (
        "dwd_order_detail_di", "ods_shop")


def test_grant_table_is_incremental_in_file_mode(tmp_path):
    """审批授予不能清掉该用户其他表的授权（覆盖式写入会放大权限并抹掉有效期）。"""
    reg = DbAccessRegistry(path=tmp_path / "db_access.json", use_db=False)
    reg.set_tables("analyst", ["orders"], "admin")
    got = reg.grant_table("analyst", "refunds", granted_by="admin",
                          expires_at=datetime.now() + timedelta(days=7))

    assert got == {"orders", "refunds"}
    assert reg.tables_for("analyst", refresh=True) == {"orders", "refunds"}


def test_duration_column_and_status_check_declared():
    from app.db.models import AccessRequest, DbAccess

    assert "expires_at" in DbAccess.__table__.columns
    cols = set(AccessRequest.__table__.columns.keys())
    assert {"requester", "table_name", "status", "duration_days",
            "reviewed_by", "review_note", "expires_at"} <= cols


@pytest.mark.asyncio
async def test_create_route_rejects_unknown_table(monkeypatch):
    """表不存在时返回 400，而不是让管理员审批一张不存在的表。"""
    from fastapi import HTTPException

    from app.api import routes_access
    from app.services import access_requests
    from app.services.auth import AuthUser

    def boom(*args, **kwargs):
        raise ValueError("未找到数据表：_no_such_table")

    monkeypatch.setattr(access_requests, "create", boom)
    user = AuthUser(id=1, username="analyst", display_name="分析师", role="user",
                    permissions=["chat.use"])
    with pytest.raises(HTTPException) as exc:
        await routes_access.create_request(
            routes_access.AccessIn(table_name="_no_such_table"), user=user)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_catalog_lists_table_names_only(monkeypatch):
    """申请人要能选到表名；目录只给表名+中文名，不含字段结构。"""
    from app.api import routes_access
    from app.services import metadata_store as ms_module
    from app.services.auth import AuthUser

    class _Store:
        @staticmethod
        def tables():
            return [{"table_name": "b", "description": "乙表", "schema": "dwd_shop",
                     "columns": ["secret_col"]},
                    {"table_name": "a", "cn_name": ""},
                    {"table_name": "", "cn_name": "空表名"}]

    monkeypatch.setattr(ms_module, "metadata_store", _Store())
    user = AuthUser(id=1, username="analyst", display_name="分析师", role="user",
                    permissions=["chat.use"])
    data = await routes_access.catalog(_=user)

    assert data["items"] == [{"table_name": "a", "cn_name": ""},
                             {"table_name": "b", "cn_name": "乙表"}]
    assert all(set(i) == {"table_name", "cn_name"} for i in data["items"])


@pytest.mark.asyncio
async def test_approve_route_maps_missing_request_to_404(monkeypatch):
    from fastapi import HTTPException

    from app.api import routes_access
    from app.services import access_requests
    from app.services.auth import AuthUser

    def missing(*args, **kwargs):
        raise LookupError("申请不存在")

    monkeypatch.setattr(access_requests, "decide", missing)
    admin = AuthUser(id=1, username="admin", display_name="管理员", role="admin",
                     permissions=[])
    with pytest.raises(HTTPException) as exc:
        await routes_access.approve("AR-9999", routes_access.DecisionIn(), user=admin)
    assert exc.value.status_code == 404
