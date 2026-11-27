"""LLM API 注册表（B16）：管理员管理接入的大模型 API。

存储：业务库 `llm_api` 表优先（0014）；库不可用时降级 `data/llm_apis.json` 文件兜底
（该文件已删除、按需自动重建，仅存放新写入的条目，不承载历史配置）。
- 首次读到空表时自动种子化（优先 .env 默认配置），避免升级丢模型配置；
- 库优先后**不再双写文件**，避免两份真相；降级期间写文件，库恢复后以库为准；
- 库故障只熔断 30 秒后重试，不做「一次失败永久降级」；
- 0017 起不存明文 Key：条目存密钥引用（api_key_ref，env:/file:/kms://），读取时由
  secret_ref 解析；明文只存在于内存调用链，列表 API 只返回脱敏尾号与健康状态；
- 引用解析失败按「未配置」fail-closed 处理，绝不静默降级到明文。
"""
import ipaddress
import logging
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.secret_ref import SecretRefError, extract_plaintext, health, resolve, validate_ref
from app.services.shared_json import SharedJson

logger = logging.getLogger("datapro.llm")

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "llm_apis.json"
_DB_RETRY_SECONDS = 30.0


def mask_key(key: str) -> str:
    return "*" * 12 + key[-4:] if key and len(key) > 4 else "（未配置）"


class LLMRegistry:
    def __init__(self, use_db: bool = True):
        self._use_db = use_db
        self._lock = threading.RLock()
        self._db_retry_at = 0.0

    # ---------- 存储后端 ----------
    @staticmethod
    def _store() -> SharedJson:
        return SharedJson(DATA_FILE, default=[])

    def _db_ready(self) -> bool:
        return self._use_db and time.monotonic() >= self._db_retry_at

    def _db_failed(self, exc: Exception) -> None:
        # 熔断而非永久禁用：库恢复后最多 30 秒重新尝试
        self._db_retry_at = time.monotonic() + _DB_RETRY_SECONDS
        logger.warning("LLM API 表不可用，降级文件（30 秒后重试）：%s", exc)

    @staticmethod
    def _to_dict(row) -> dict:
        return {
            "id": row.id, "name": row.name, "base_url": row.base_url,
            "api_key_ref": row.api_key_ref or "", "model": row.model,
            "enabled": bool(row.enabled), "is_default": bool(row.is_default),
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        }

    @staticmethod
    def _hydrate(entry: dict) -> dict:
        """把库/文件条目水合为可调用形态：解析引用得到 api_key（仅内存）与健康状态。

        兼容旧文件条目：若有明文 api_key 而无引用，自动搬到密钥文件并改写为 file: 引用。
        """
        entry = dict(entry)
        ref = (entry.get("api_key_ref") or "").strip()
        legacy = (entry.pop("api_key", "") or "").strip()      # 旧格式文件条目
        if not ref and legacy:
            ref = extract_plaintext(legacy, f"LLM_KEY_{str(entry.get('id') or 'x').upper()}")
            entry["api_key_ref"] = ref
            logger.info("条目 %s 的明文 Key 已转为密钥文件引用，请尽快改用 env:/kms: 引用",
                        entry.get("id"))
        entry["api_key_ref"] = ref
        h = health(ref)
        entry["key_state"] = h["state"]
        entry["key_detail"] = h["detail"]
        if h["state"] == "ok":
            entry["api_key"] = resolve(ref)
        else:
            entry.pop("api_key", None)
        return entry

    def _read_db(self) -> list[dict] | None:
        if not self._db_ready():
            return None
        try:
            from app.db.models import LLMApi
            from app.db.session import SessionLocal

            with SessionLocal() as db:
                rows = db.query(LLMApi).order_by(LLMApi.created_at, LLMApi.id).all()
                return [self._hydrate(self._to_dict(r)) for r in rows]
        except Exception as exc:  # noqa: BLE001 —— 库不可用时读文件
            self._db_failed(exc)
            return None

    def _seed_db(self, items: list[dict]) -> None:
        """空表种子化：只插不覆盖，重复调用安全。旧明文条目先转引用。"""
        if not items:
            return
        try:
            from app.db.models import LLMApi
            from app.db.session import SessionLocal

            with SessionLocal() as db:
                existing = {r[0] for r in db.query(LLMApi.id).all()}
                for a in items:
                    if str(a.get("id")) in existing:
                        continue
                    hydrated = self._hydrate(dict(a))
                    db.add(LLMApi(
                        id=str(a.get("id") or uuid.uuid4().hex[:8]),
                        name=str(a.get("name") or "未命名"),
                        base_url=str(a.get("base_url") or settings.llm_base_url),
                        api_key_ref=str(hydrated.get("api_key_ref") or ""),
                        model=str(a.get("model") or settings.llm_model),
                        enabled=bool(a.get("enabled", True)),
                        is_default=bool(a.get("is_default", False)),
                    ))
                db.commit()
        except Exception as exc:  # noqa: BLE001 —— 种子失败不影响读文件兜底
            self._db_failed(exc)

    def _default_from_env(self) -> list[dict]:
        ref = "env:LLM_API_KEY" if settings.llm_api_key else ""
        return [{
            "id": uuid.uuid4().hex[:8],
            "name": "环境默认（.env）",
            "base_url": settings.llm_base_url,
            "api_key_ref": ref,
            "model": settings.llm_model,
            "enabled": True,
            "is_default": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }]

    def _read_all(self) -> list[dict]:
        if self._use_db:
            rows = self._read_db()
            if rows is not None:
                if rows:
                    return rows
                # 空表：优先沿用历史 JSON（升级不丢配置），否则用 .env 初始化
                legacy = self._store().snapshot() if DATA_FILE.exists() else []
                self._seed_db(legacy or self._default_from_env())
                return self._read_db() or []
        return [self._hydrate(a) for a in self._store().snapshot()]

    def _public(self, a: dict) -> dict:
        """对外形态：引用与健康状态可见，明文 Key 绝不出服务层。"""
        return {k: a[k] for k in ("id", "name", "base_url", "model", "enabled",
                                  "is_default", "created_at", "api_key_ref",
                                  "key_state", "key_detail")} \
            | {"key_masked": mask_key(a.get("api_key", ""))}

    # ---------- 读取 ----------
    def list(self) -> list[dict]:
        return [self._public(a) for a in self._read_all()]

    def get(self, api_id: str) -> dict | None:
        return next((dict(a) for a in self._read_all() if a["id"] == api_id), None)

    def default_active(self) -> dict | None:
        """当前生效的默认 API（B17：运行时读取，管理员切换后下一次请求即生效）。"""
        a = next((x for x in self._read_all() if x["is_default"] and x["enabled"]), None)
        return dict(a) if a else None

    def status(self) -> dict:
        """普通用户可见的只读状态。"""
        apis = self._read_all()
        d = next((a for a in apis if a["is_default"] and a["enabled"]), None)
        return {
            "default_model": d["model"] if d else None,
            "provider": d["name"] if d else None,
            # 可用 = 有默认启用项且密钥引用解析成功（否则生成走 mock 降级）
            "available": bool(d and d.get("key_state") == "ok"),
            "key_state": d.get("key_state") if d else None,
            "total_apis": len(apis),
        }

    # ---------- 写入 ----------
    @staticmethod
    def validate_base_url(url: str) -> str:
        """仅允许公网 https 端点，防 SSRF（管理员专属能力仍需防内网探测）。"""
        parts = urlsplit((url or "").strip())
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("API 地址必须是 https，且不能包含用户名密码")
        try:
            addresses = socket.getaddrinfo(parts.hostname, parts.port or 443,
                                           type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("API 地址无法解析") from exc
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                    or ip.is_multicast or ip.is_unspecified:
                raise ValueError("API 地址不能指向内网或本地地址")
        return url.rstrip("/")

    def add(self, data: dict) -> dict:
        if data.get("api_key"):
            raise ValueError("不支持明文 Key：请传 api_key_ref（如 env:LLM_API_KEY），"
                             "或先用密钥文件/环境变量保管 Key")
        safe_url = self.validate_base_url(data["base_url"])
        key_ref = validate_ref(data.get("api_key_ref", ""))
        entry = {
            "id": uuid.uuid4().hex[:8],
            "name": data["name"],
            "base_url": safe_url,
            "api_key_ref": key_ref,
            "model": data["model"],
            "enabled": data.get("enabled", True),
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        if self._db_ready():
            try:
                from app.db.models import LLMApi
                from app.db.session import SessionLocal

                with SessionLocal() as db:
                    entry["is_default"] = db.query(LLMApi).count() == 0  # 首个自动设默认
                    db.add(LLMApi(**{k: entry[k] for k in
                                     ("id", "name", "base_url", "api_key_ref", "model",
                                      "enabled", "is_default")}))
                    db.commit()
                    return self._public(self._hydrate(entry))
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级文件
                self._db_failed(exc)
        with self._store().edit() as apis:
            entry["is_default"] = not apis
            apis.append({k: entry[k] for k in
                         ("id", "name", "base_url", "api_key_ref", "model",
                          "enabled", "is_default", "created_at")})
            return self._public(self._hydrate(entry))

    def update(self, api_id: str, patch: dict) -> dict | None:
        if patch.get("api_key"):
            raise ValueError("不支持明文 Key：请传 api_key_ref（如 env:LLM_API_KEY）")
        if "api_key_ref" in patch:
            patch = {**patch, "api_key_ref": validate_ref(patch["api_key_ref"])}
        if self._db_ready():
            try:
                from app.db.models import LLMApi
                from app.db.session import SessionLocal

                with SessionLocal() as db:
                    row = db.get(LLMApi, api_id)
                    if row is None:
                        return None
                    for k in ("name", "model", "enabled"):
                        if k in patch:
                            setattr(row, k, patch[k])
                    if "base_url" in patch:
                        row.base_url = self.validate_base_url(patch["base_url"])
                    if "api_key_ref" in patch:      # 空字符串表示清除引用
                        row.api_key_ref = patch["api_key_ref"]
                    db.commit()
                    db.refresh(row)
                    return self._public(self._hydrate(self._to_dict(row)))
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级文件
                self._db_failed(exc)
        with self._store().edit() as apis:
            for a in apis:
                if a["id"] == api_id:
                    for k in ("name", "base_url", "model", "enabled"):
                        if k in patch:
                            a[k] = self.validate_base_url(patch[k]) if k == "base_url" else patch[k]
                    if "api_key_ref" in patch:
                        a["api_key_ref"] = patch["api_key_ref"]
                    return self._public(self._hydrate(a))
        return None

    def delete(self, api_id: str) -> bool:
        if self._db_ready():
            try:
                from app.db.models import LLMApi
                from app.db.session import SessionLocal

                with SessionLocal() as db:
                    row = db.get(LLMApi, api_id)
                    if row is None:
                        return False
                    db.delete(row)
                    db.flush()
                    remaining = db.query(LLMApi).order_by(LLMApi.created_at, LLMApi.id).all()
                    if remaining and not any(r.is_default for r in remaining):
                        remaining[0].is_default = True     # 删除默认项时顺延第一个
                    db.commit()
                    return True
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级文件
                self._db_failed(exc)
        with self._store().edit() as apis:
            kept = [a for a in apis if a["id"] != api_id]
            if len(kept) == len(apis):
                return False
            apis[:] = kept
            if apis and not any(a["is_default"] for a in apis):
                apis[0]["is_default"] = True
            return True

    def set_default(self, api_id: str) -> bool:
        if self._db_ready():
            try:
                from app.db.models import LLMApi
                from app.db.session import SessionLocal

                with SessionLocal() as db:
                    rows = db.query(LLMApi).all()
                    if not any(r.id == api_id for r in rows):
                        return False
                    for r in rows:
                        r.is_default = r.id == api_id
                    db.commit()
                    return True
            except Exception as exc:  # noqa: BLE001 —— 写库失败降级文件
                self._db_failed(exc)
        with self._store().edit() as apis:
            hit = False
            for a in apis:
                a["is_default"] = a["id"] == api_id
                hit = hit or a["id"] == api_id
            return hit

    async def test(self, api_id: str) -> dict:
        """连通性测试：向该 API 发一次最小对话请求。"""
        a = self.get(api_id)
        if not a:
            return {"ok": False, "message": "API 不存在"}
        if not a.get("api_key"):
            detail = a.get("key_detail") or "未配置密钥引用"
            return {"ok": False, "message": f"Key 不可用（{detail}）"}
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
                    "message": f"HTTP {resp.status_code}（响应正文不回显）"}
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}"}


llm_registry = LLMRegistry()
