"""Langfuse 可观测单测：未配置时 no-op、配置后用本地 mock 服务验证上报。"""
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings
from app.services import tracing

BODIES: list[tuple[str, bytes]] = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        BODIES.append((self.path, self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(autouse=True)
def _reset_tracing():
    BODIES.clear()
    tracing.reset()
    yield
    tracing.reset()


def test_noop_when_not_configured():
    assert not tracing.enabled()
    with tracing.trace("查昨天的支付订单数", session_id="s", user_id="u") as tr:
        assert tr.trace_id is None
        tr.event("node:INTENT", metadata={"detail": "query"})
        with tracing.generation("llm:sql_gen", "deepseek-flash", {"prompt": "x"}) as gen:
            gen.record(output={"sql": "SELECT 1"})
        tr.finish(output={"summary": "43"})
    tracing.score(None, "user_feedback", 1.0)
    tracing.flush()


def test_reports_to_langfuse_endpoint(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(settings, "langfuse_host", host)

    assert tracing.enabled()
    with tracing.trace("查昨天的支付订单数", session_id="s1", user_id="alice",
                       tags=["query"]) as tr:
        tr.event("node:INTENT", metadata={"detail": "query"})
        with tracing.generation("llm:sql_gen", "deepseek-flash", {"prompt": "生成SQL"}) as gen:
            gen.record(output={"sql": "SELECT 1"}, usage={"input": 10, "output": 5, "total": 15})
        tr.finish(output={"summary": "43"})
        assert tr.trace_id
    tracing.score(tr.trace_id, "user_feedback", 1.0)
    tracing.flush()
    server.shutdown()

    assert BODIES, "SDK 未向 Langfuse 上报"
    assert all(path.startswith("/api/public/") for path, _ in BODIES)
    trace_bytes = b"".join(body for path, body in BODIES if "otel" in path)
    spans = gzip.decompress(trace_bytes)          # OTLP 以 gzip protobuf 上报
    assert b"chat" in spans and b"llm:sql_gen" in spans
    scores = [json.loads(body) for path, body in BODIES if path.endswith("/scores")]
    assert any(item.get("name") == "user_feedback"
               for batch in scores for item in (batch if isinstance(batch, list) else [batch]))
