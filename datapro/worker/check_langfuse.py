"""Langfuse 连通性自检（F48）：服务端是否可达 + DataPro 能否上报一条 trace。

用法（先配好 LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY）：
  python -m worker.check_langfuse
"""
import base64
import json
import sys
import urllib.error
import urllib.request

from app.config import settings
from app.services import tracing


def _mask(value: str) -> str:
    return (value[:6] + "***") if value else "（空）"


def check_health() -> tuple[bool, str]:
    url = settings.langfuse_host.rstrip("/") + "/api/public/health"
    req = urllib.request.Request(url)
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        token = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200, resp.read().decode("utf-8", errors="ignore")[:200]
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 —— 自检脚本直接打印原因
        return False, str(exc)


def main() -> int:
    print(f"LANGFUSE_ENABLED      = {settings.langfuse_enabled}")
    print(f"LANGFUSE_HOST         = {settings.langfuse_host}")
    print(f"LANGFUSE_PUBLIC_KEY   = {_mask(settings.langfuse_public_key)}")
    print(f"LANGFUSE_SECRET_KEY   = {_mask(settings.langfuse_secret_key)}")

    ok, detail = check_health()
    print(f"服务端健康检查         = {'OK' if ok else 'FAIL'} {detail}")
    if not tracing.enabled():
        print("DataPro 上报未启用：需要 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 且开关打开")
        return 2 if ok else 1

    with tracing.trace("DataPro Langfuse 连通性自检", session_id="healthcheck",
                       user_id="system") as t:
        t.event("healthcheck", ok=ok)
        t.finish(output={"ok": ok, "health": detail})
        print(f"trace_id              = {t.trace_id or '（未获取到，上报可能失败）'}")
    tracing.flush()
    print("已 flush；到 Langfuse 项目 Tracing 页确认是否出现「连通性自检」")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
