"""IM webhook 通知（巡检告警）：飞书 / 钉钉 / 企业微信，可选签名。

配置：
- `INSPECTION_WEBHOOK_URL`：机器人地址
- `INSPECTION_WEBHOOK_TYPE`：auto | feishu | dingtalk | wecom | generic
- `INSPECTION_WEBHOOK_SECRET`：钉钉加签密钥 / 飞书签名校验密钥（可空）

`auto` 按 URL 识别平台，识别不出按飞书格式发送。发送失败只记日志，不抛异常。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("datapro.webhook")

VALID_TYPES = ("auto", "feishu", "dingtalk", "wecom", "generic")


def detect_type(url: str) -> str:
    u = (url or "").lower()
    if "dingtalk" in u:
        return "dingtalk"
    if "qyapi.weixin.qq.com" in u or "wecom" in u:
        return "wecom"
    if "feishu" in u or "larksuite" in u or "lark" in u:
        return "feishu"
    return "feishu"


def _feishu_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名：以 `timestamp\\nsecret` 为 key，对空串做 HMAC-SHA256。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dingtalk_sign(timestamp: str, secret: str) -> str:
    """钉钉加签：以 secret 为 key，对 `timestamp\\nsecret` 做 HMAC-SHA256 后 base64。"""
    key = secret.encode("utf-8")
    msg = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, msg, digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))


def build_request(url: str, text: str, kind: str = "auto", secret: str = "") -> tuple[str, dict]:
    """返回（最终 URL, JSON payload）。"""
    kind = kind if kind in VALID_TYPES else "auto"
    if kind == "auto":
        kind = detect_type(url)

    if kind == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": text}}
        if secret:
            ts = str(round(time.time() * 1000))
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={_dingtalk_sign(ts, secret)}"
        return url, payload

    if kind == "wecom":
        return url, {"msgtype": "text", "text": {"content": text}}

    if kind == "generic":
        return url, {"text": text}

    # 飞书 / Lark
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(round(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(ts, secret)
    return url, payload


def send_text(url: str, text: str, kind: str = "auto", secret: str = "") -> bool:
    """发送文本；返回是否成功，异常只记日志。"""
    if not (url or "").strip():
        return False
    final_url, payload = build_request(url.strip(), text, kind, secret)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(final_url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        # 钉钉/企微可能 HTTP 200 但业务码失败，尽量把错误记下来
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        code = data.get("errcode", data.get("code", 0))
        if code not in (0, "0", None):
            logger.warning("IM webhook 返回业务错误：%s", raw[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 —— 通知失败不影响巡检结果
        logger.warning("IM webhook 发送失败：%s", exc)
        return False
