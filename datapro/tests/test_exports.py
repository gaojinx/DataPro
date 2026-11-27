"""导出申请与审批（B13/F11）单测：状态流转 + 审批后生成 CSV。"""
import csv

import pytest

from app.services import export_registry as mod
from app.services.export_registry import ExportRegistry


def _svc(tmp_path, monkeypatch) -> ExportRegistry:
    monkeypatch.setattr(mod, "INDEX_PATH", tmp_path / "exports.json")
    monkeypatch.setattr(mod, "EXPORT_DIR", tmp_path / "files")
    return ExportRegistry(use_db=False)


def test_create_and_list_own(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di LIMIT 200", row_count=200, reason="对账")
    assert rec["id"] == "EP-0001" and rec["status"] == "pending"
    svc.create("bob", "导出用户", "SELECT 2")
    assert [r["id"] for r in svc.list("alice")] == ["EP-0001"]
    assert len(svc.list(all_users=True)) == 2


def test_decide_flow_and_duplicate_guard(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di LIMIT 200")
    approved = svc.decide(rec["id"], "approve", "admin", "同意")
    assert approved["status"] == "approved" and approved["reviewed_by"] == "admin"
    with pytest.raises(ValueError):
        svc.decide(rec["id"], "reject", "admin")
    with pytest.raises(KeyError):
        svc.decide("EP-9999", "approve", "admin")


def test_create_rejects_empty_sql(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        svc.create("alice", "空查询", "   ")


async def test_run_export_writes_csv(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di LIMIT 200", session_id="s1")
    svc.decide(rec["id"], "approve", "admin")

    from app.services import executor as executor_mod

    async def fake_fetch_all(sql, user_id, session_id="", window=None, max_rows=None):
        return {"columns": ["order_id", "amount"], "rows": [
            {"order_id": "A1", "amount": 12.5}, {"order_id": "A2", "amount": 8}],
            "row_count": 2, "truncated": False}

    monkeypatch.setattr(executor_mod.executor_service, "fetch_all", fake_fetch_all)
    updated = await svc.run_export(rec["id"])
    assert updated["status"] == "approved" and updated["row_count"] == 2
    path = svc.file_path(rec["id"])
    assert path and path.exists()
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["order_id", "amount"] and rows[1] == ["A1", "12.5"]


async def test_run_export_requires_approval(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di LIMIT 200")
    with pytest.raises(ValueError):
        await svc.run_export(rec["id"])


async def test_run_export_uses_original_window(tmp_path, monkeypatch):
    """导出重跑必须沿用原查询时间窗，不能退回默认最近 7 天。"""
    svc = _svc(tmp_path, monkeypatch)
    rec = svc.create("alice", "导出订单", "SELECT order_id FROM dwd_order_detail_di WHERE dt BETWEEN '{date_start}' AND '{date_end}' LIMIT 200",
                     window_start="2026-11-16", window_end="2026-11-22")
    assert rec["window_start"] == "2026-11-16" and rec["window_end"] == "2026-11-22"
    svc.decide(rec["id"], "approve", "admin")

    from app.services import executor as executor_mod
    captured = {}

    async def fake_fetch_all(sql, user_id, session_id="", window=None, max_rows=None):
        captured["start"], captured["end"] = window.start, window.end
        return {"columns": ["x"], "rows": [{"x": 1}], "row_count": 1, "truncated": False}

    monkeypatch.setattr(executor_mod.executor_service, "fetch_all", fake_fetch_all)
    await svc.run_export(rec["id"])
    assert str(captured["start"]) == "2026-11-16" and str(captured["end"]) == "2026-11-22"
