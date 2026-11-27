"""IM webhook 适配：飞书 / 钉钉 / 企业微信 payload 与签名。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.services.webhook import build_request, detect_type, send_text


def test_detect_type():
    assert detect_type("https://open.feishu.cn/open-apis/bot/v2/hook/xxx") == "feishu"
    assert detect_type("https://oapi.dingtalk.com/robot/send?access_token=x") == "dingtalk"
    assert detect_type("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x") == "wecom"
    assert detect_type("http://127.0.0.1/hook") == "feishu"


def test_feishu_payload_with_sign():
    url, payload = build_request("https://open.feishu.cn/hook", "告警", "feishu", "secret")
    assert payload["msg_type"] == "text" and payload["content"]["text"] == "告警"
    assert payload["timestamp"] and payload["sign"]


def test_dingtalk_payload_with_sign():
    url, payload = build_request("https://oapi.dingtalk.com/robot/send?access_token=x",
                                 "告警", "dingtalk", "secret")
    assert payload == {"msgtype": "text", "text": {"content": "告警"}}
    assert "timestamp=" in url and "sign=" in url


def test_wecom_and_generic_payload():
    _, wecom = build_request("https://qyapi.weixin.qq.com/hook", "告警", "wecom")
    assert wecom == {"msgtype": "text", "text": {"content": "告警"}}
    _, generic = build_request("http://x/hook", "告警", "generic")
    assert generic == {"text": "告警"}


def test_send_text_posts_and_reads_errcode():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received["body"] = json.loads(self.rfile.read(length))
            received["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"errcode": 0}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        ok = send_text(f"http://127.0.0.1:{server.server_port}/hook", "巡检失败",
                       kind="dingtalk", secret="s3cret")
        thread.join(timeout=5)
    finally:
        server.server_close()
    assert ok is True
    assert received["body"] == {"msgtype": "text", "text": {"content": "巡检失败"}}
    assert "sign=" in received["path"]
