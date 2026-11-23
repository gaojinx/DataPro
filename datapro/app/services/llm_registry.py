"""LLM API 注册表（功能清单 B16）：管理员管理接入的大模型 API。

- 骨架用 JSON 文件持久化（data/llm_apis.json）；生产应接入密钥管理服务（B12 评审项）
- 列表返回脱敏 Key（仅尾 4 位），原始 Key 不出服务层
- RBAC 在路由层实现：仅管理员可写，普通用户走 /api/v1/llm/status 只读
"""
import json
import threading
import time
import uuid
from pathlib import Path

import httpx

from app.config import settings

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "llm_apis.json"


def mask_key(key: str) -> str:
    return "*" * 12 + key[-4:] if key and len(key) > 4 else "（未配置）"


class LLMRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        DATA_FILE.parent.mkdir(exist_ok=True)
        self._apis: list[dict] = []
        if DATA_FILE.exists():
            self._apis = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        else:
            # 首次启动：用 .env 配置初始化默认 API 条目
            self._apis = [{
                "id": uuid.uuid4().hex[:8],
                "name": "环境默认（.env）",
                "base_url": settings.llm_base_url,
                "api_key": settings.llm_api_key,
                "model": settings.llm_model,
                "enabled": True,
                "is_default": True,
                "created_at": time.strftime("%Y-%m-%d %H:%M"),
            }]
            self._save()

    def _save(self):
        DATA_FILE.write_text(json.dumps(self._apis, ensure_ascii=False, indent=2), encoding="utf-8")

    def _public(self, a: dict) -> dict:
        return {k: a[k] for k in ("id", "name", "base_url", "model", "enabled", "is_default", "created_at")} \
            | {"key_masked": mask_key(a.get("api_key", ""))}

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(a) for a in self._apis]

    def add(self, data: dict) -> dict:
        with self._lock:
            entry = {
                "id": uuid.uuid4().hex[:8],
                "name": data["name"],
                "base_url": data["base_url"].rstrip("/"),
                "api_key": data.get("api_key", ""),
                "model": data["model"],
                "enabled": data.get("enabled", True),
                "is_default": not self._apis,  # 首个自动设默认
                "created_at": time.strftime("%Y-%m-%d %H:%M"),
            }
            self._apis.append(entry)
            self._save()
            return self._public(entry)

    def update(self, api_id: str, patch: dict) -> dict | None:
        with self._lock:
            for a in self._apis:
                if a["id"] == api_id:
                    for k in ("name", "base_url", "model", "enabled"):
                        if k in patch:
                            a[k] = patch[k]
                    if patch.get("api_key"):  # 留空表示不修改 Key
                        a["api_key"] = patch["api_key"]
                    self._save()
                    return self._public(a)
            return None

    def delete(self, api_id: str) -> bool:
        with self._lock:
            before = len(self._apis)
            self._apis = [a for a in self._apis if a["id"] != api_id]
            if len(self._apis) < before:
                # 删除默认项时顺延第一个
                if self._apis and not any(a["is_default"] for a in self._apis):
                    self._apis[0]["is_default"] = True
                self._save()
                return True
            return False

    def set_default(self, api_id: str) -> bool:
        with self._lock:
            hit = False
            for a in self._apis:
                a["is_default"] = a["id"] == api_id
                hit = hit or a["id"] == api_id
            if hit:
                self._save()
            return hit

    def default_active(self) -> dict | None:
        """当前生效的默认 API（B17：运行时读取，管理员切换后下一次请求即生效）。"""
        with self._lock:
            a = next((x for x in self._apis if x["is_default"] and x["enabled"]), None)
            return dict(a) if a else None

    def status(self) -> dict:
        """普通用户可见的只读状态。"""
        with self._lock:
            d = next((a for a in self._apis if a["is_default"] and a["enabled"]), None)
            return {
                "default_model": d["model"] if d else None,
                "provider": d["name"] if d else None,
                # 可用 = 有默认启用项且已配置 Key（否则生成走 mock 降级）
                "available": bool(d and d.get("api_key")),
                "total_apis": len(self._apis),
            }

    async def test(self, api_id: str) -> dict:
        """连通性测试：向该 API 发一次最小对话请求。"""
        with self._lock:
            a = next((x for x in self._apis if x["id"] == api_id), None)
        if not a:
            return {"ok": False, "message": "API 不存在"}
        if not a.get("api_key"):
            return {"ok": False, "message": "API Key 未配置"}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{a['base_url'].rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {a['api_key']}"},
                    json={"model": a["model"], "max_tokens": 4,
                          "messages": [{"role": "user", "content": "ping"}]},
                )
            latency = round((time.monotonic() - started) * 1000)
            if resp.status_code == 200:
                return {"ok": True, "latency_ms": latency, "message": f"连通正常（{latency}ms）"}
            return {"ok": False, "latency_ms": latency,
                    "message": f"HTTP {resp.status_code}: {resp.text[:120]}"}
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}"}


llm_registry = LLMRegistry()
