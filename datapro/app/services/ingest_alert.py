"""采集告警：薄封装 webhook.send_text，保证发出去的文本无明文口令、无 HTML 原文。

复用 INSPECTION_WEBHOOK_URL/TYPE/SECRET；发送失败只记日志，不影响采集主链路。
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Mapping

from app.config import settings
from app.services import webhook

logger = logging.getLogger("datapro.ingest.alert")

# 只保留原因码与统计值；batch 里的 error 字段本身就只允许写原因码。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(xianzhi_password|xianzhi_username|password|passwd|pwd|secret|token|api[_-]?key)\s*=\s*\S+"),
    re.compile(r"(?i)sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"https?://\S+"),
)


def sanitize_text(text: Any, limit: int = 400) -> str:
    """脱敏：剥 HTML 标签、去凭据赋值与 URL/token、截断。"""
    value = "" if text is None else str(text)
    value = re.sub(r"<[^>]*>", " ", value)
    value = html.unescape(value)
    for pattern in _SENSITIVE_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def build_message(batch: Mapping[str, Any] | None, reason: str) -> str:
    """按白名单字段拼告警文本，绝不把 batch.error / raw_row 原文带进来。"""
    batch = batch or {}
    reason_code = sanitize_text(reason, 80) or "UNKNOWN"
    window = ""
    if batch.get("window_start") and batch.get("window_end"):
        window = f"，窗口 {batch['window_start']}~{batch['window_end']}"
    rows_loaded = batch.get("rows_loaded", 0)
    rows_rejected = batch.get("rows_rejected", 0)
    return (f"先知采集告警：{reason_code}{window}；"
            f"rows_loaded={rows_loaded}，rows_rejected={rows_rejected}；"
            f"batch_no={sanitize_text(batch.get('batch_no', ''), 40) or '-'}")


class IngestAlertService:
    """采集告警服务：notify() 永不抛异常，返回是否发送成功。"""

    def __init__(self, url: str | None = None, kind: str | None = None,
                 secret: str | None = None) -> None:
        self.url = settings.inspection_webhook_url if url is None else url
        self.kind = settings.inspection_webhook_type if kind is None else kind
        self.secret = settings.inspection_webhook_secret if secret is None else secret

    def notify(self, batch: Mapping[str, Any] | None, reason: str) -> bool:
        message = build_message(batch, reason)
        if "<" in message or "***" in message:
            # 双保险：告警文本里不应出现 HTML 标签或占位符以外的敏感形态
            logger.warning("告警文本未通过脱敏校验，已拦截")
            return False
        try:
            return webhook.send_text(self.url, message, self.kind, self.secret)
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集告警发送失败：%s", type(exc).__name__)
            return False
