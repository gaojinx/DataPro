"""巡检服务（A08）单测：规则持久化、校验与三类检查判定。"""
import json
from datetime import date, timedelta

import pytest

from app.services import inspection as mod
from app.services.inspection import InspectionService


class _Result:
    def __init__(self, scalar=None, row=None):
        self._scalar, self._row = scalar, row

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    """按 SQL 文本返回预置结果的假连接：latest / prev / count / null 统计。"""

    def __init__(self, latest, prev, count=100, nulls=0, counts=None):
        self.latest, self.prev, self.count, self.nulls = latest, prev, count, nulls
        self.counts = counts or {}

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "MAX(dt)" in sql and "WHERE dt <" in sql:
            return _Result(scalar=self.prev)
        if "MAX(dt)" in sql:
            return _Result(scalar=self.latest)
        if "SUM(" in sql:
            return _Result(row={"n": self.nulls, "c": self.count})
        if self.counts and params:
            return _Result(scalar=self.counts.get(params.get("d"), self.count))
        return _Result(scalar=self.count)


class _CmConn:
    """给假连接补上上下文管理器协议（run_all 用 with engine.connect()）。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        return False


def _svc(tmp_path, monkeypatch) -> InspectionService:
    monkeypatch.setattr(mod, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(mod, "RESULTS_PATH", tmp_path / "results.json")
    return InspectionService()


def test_default_rules_written_on_first_read(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    rules = svc.rules()
    assert len(rules) == 4
    assert (tmp_path / "rules.json").exists()
    assert {r["check"] for r in rules} == {"rowcount_drop", "null_ratio", "no_data"}


def test_save_rules_validates(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        svc.save_rules([{"id": "X1", "table": "t", "check": "unknown"}])
    with pytest.raises(ValueError):
        svc.save_rules([
            {"id": "X1", "table": "t", "check": "no_data"},
            {"id": "X1", "table": "t", "check": "no_data"},
        ])
    saved = svc.save_rules([{"id": "X1", "table": "dwd_user_di", "check": "no_data",
                             "threshold": "2", "enabled": True}])
    assert saved[0]["threshold"] == 2.0
    assert svc.rules()[0]["id"] == "X1"


def test_checks_pass_and_fail(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    today = date.today()
    latest, prev = today - timedelta(days=1), today - timedelta(days=2)

    drop = svc._run_rule(_Conn(latest, prev, counts={latest: 10, prev: 100}), {
        "id": "R1", "table": "t", "check": "rowcount_drop", "threshold": 0.5})
    assert drop["status"] == "fail"          # 10 vs 100 行，下降 90%
    nulls = svc._run_rule(_Conn(latest, prev, 100, nulls=10), {
        "id": "R2", "table": "t", "check": "null_ratio", "column": "amount",
        "threshold": 0.05})
    assert nulls["status"] == "fail"         # 空值率 10%
    fresh = svc._run_rule(_Conn(today, prev, 100), {
        "id": "R3", "table": "t", "check": "no_data", "threshold": 1})
    assert fresh["status"] == "pass"         # 最新分区就是今天
    stale = svc._run_rule(_Conn(today - timedelta(days=3), prev, 100), {
        "id": "R4", "table": "t", "check": "no_data", "threshold": 1})
    assert stale["status"] == "fail"


def test_run_all_skipped_when_disabled(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.settings, "feature_inspection_enabled", False)
    assert svc.run_all("test")["skipped"] is True


def test_summary_reads_latest_result(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    (tmp_path / "results.json").write_text(json.dumps({
        "updated_at": "2026-11-24 14:00:00",
        "items": [{"ts": "2026-11-24 14:00:00", "items": [
            {"status": "fail"}, {"status": "pass"}, {"status": "error"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    summary = svc.summary()
    assert summary["latest_total"] == 3
    assert summary["latest_failed"] == 1 and summary["latest_error"] == 1


def test_webhook_posts_failure_payload(monkeypatch):
    """巡检失败告警：按 IM 机器人文本格式 POST，payload 含规则与说明。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    monkeypatch.setattr(mod.settings, "inspection_webhook_url",
                        f"http://127.0.0.1:{server.server_port}/hook")
    try:
        mod.InspectionService._notify_failures(
            "2026-11-24 06:00:00",
            [{"rule_id": "R001", "table": "dwd_order_detail_di", "message": "行数骤降"}])
        thread.join(timeout=5)
    finally:
        server.server_close()
    body = received.get("body") or {}
    assert body.get("msg_type") == "text"
    assert "R001" in body.get("content", {}).get("text", "")


def test_failure_increments_metrics(tmp_path, monkeypatch):
    """巡检失败写入 Prometheus 计数（I03/I04 告警依赖）。"""
    from app.services.metrics import metrics

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "rules", lambda: [{"id": "R9", "table": "t",
                                                "check": "null_ratio", "column": "c"}])
    monkeypatch.setattr(svc, "_run_rule", lambda conn, rule: {
        **svc._base(rule), "status": "fail", "message": "空值率超阈值"})
    monkeypatch.setattr(svc, "_notify_failures", lambda ts, failures: None)

    class _Engine:
        def connect(self):
            return _CmConn(_Conn(date.today(), date.today()))

        def dispose(self):
            return None

    monkeypatch.setattr(mod.sa, "create_engine", lambda *a, **k: _Engine())
    before = metrics.value("datapro_inspection_failure_total", check="null_ratio")
    result = svc.run_all("test")
    assert result["failed"] == 1
    assert metrics.value("datapro_inspection_failure_total", check="null_ratio") == before + 1
    assert metrics.value("datapro_inspection_runs_total", result="fail") >= 1
