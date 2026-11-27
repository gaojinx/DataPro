"""长期记忆（A11 自动记忆）：按用户隔离的偏好 / 术语 / 规则 / 口径约定。

- AI 判断重要后写入，自动生效、可在技能页查看/停用/删除
- JSON 持久化 `data/memories.json`
- 单人上限 `MAX_PER_USER`，超出时优先淘汰停用项，再淘汰置信度低且久未更新的
- 敏感信息（手机号 / 身份证 / 银行卡）一律拒绝写入
- 多实例加固：mtime 缓存失效 + 跨进程锁内 read-modify-write，读路径返回副本
"""
from __future__ import annotations

import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.services.shared_json import SharedJson

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "memories.json"
MAX_PER_USER = 50
MAX_CONTENT = 160
MIN_CONTENT = 4
TYPES = ("preference", "terminology", "rule", "caliber")

_PHONE = re.compile(r"1[3-9]\d{9}")
_ID_CARD = re.compile(r"\d{17}[\dXx]")
_BANK = re.compile(r"\b\d{16,19}\b")
_NORM = re.compile(r"[\s，。！？、,.!?：:；;\"'“”‘’()（）\[\]【】<>《》]+")


def normalize_content(text: str) -> str:
    return _NORM.sub("", (text or "").lower())


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class MemoryStore:
    def __init__(self, path: Path | None = None):
        self._path = path or DATA_FILE
        self._cache: list[dict] | None = None  # 兼容旧测试 patch；实际缓存由 SharedJson 管理

    def _store(self) -> SharedJson:
        return SharedJson(self._path, default=[])

    @contextmanager
    def _edit_items(self):
        """写入时返回可变的记忆列表，兼容两种历史存储形态。

        线上 `memories.json` 曾被包装成 {"memories": [...]}，而写路径直接迭代顶层对象，
        会对字符串键调用 .get 抛 AttributeError —— 记忆写入因此静默失效（读路径早已兼容）。
        这里统一取内层列表，写入后保持文件原有形态，避免与其它配置文件的裸数组格式打架。
        """
        with self._store().edit() as doc:
            if isinstance(doc, dict):
                yield doc.setdefault("memories", [])
            else:
                yield doc

    def _load(self) -> list[dict]:
        data = self._store().snapshot()
        if isinstance(data, dict):
            data = data.get("memories", [])
        return list(data) if isinstance(data, list) else []

    # ---- 查询 ----
    def list(self, username: str, all_users: bool = False) -> list[dict]:
        items = [dict(m) for m in self._load()]
        if not all_users:
            items = [m for m in items if m.get("username") == username]
        return sorted(items, key=lambda m: m.get("updated_at") or "", reverse=True)

    def for_user(self, username: str, limit: int = 8) -> list[dict]:
        """注入生成 prompt 的记忆：启用项，置信度高/更新近的优先。"""
        items = [m for m in self._load()
                 if m.get("username") == username and m.get("enabled", True)]
        items.sort(key=lambda m: (float(m.get("confidence") or 0), m.get("updated_at") or ""),
                   reverse=True)
        return [dict(m) for m in items[:limit]]

    @staticmethod
    def _trim(items: list[dict], username: str) -> list[dict]:
        mine = [m for m in items if m.get("username") == username]
        if len(mine) <= MAX_PER_USER:
            return items
        # 先淘汰停用项，再按置信度与更新时间升序淘汰
        mine.sort(key=lambda m: (bool(m.get("enabled", True)),
                                 float(m.get("confidence") or 0),
                                 m.get("updated_at") or ""))
        drop = {m["id"] for m in mine[:len(mine) - MAX_PER_USER]}
        return [m for m in items if m.get("id") not in drop]

    # ---- 写入 ----
    def add(self, username: str, type_: str, content: str,
            source: str = "", confidence: float = 0.0) -> dict | None:
        username = (username or "").strip()
        content = " ".join((content or "").split())[:MAX_CONTENT]
        if not username or len(content) < MIN_CONTENT or type_ not in TYPES:
            return None
        if _PHONE.search(content) or _ID_CARD.search(content) or _BANK.search(content):
            return None
        conf = round(float(confidence or 0), 2)
        with self._edit_items() as items:
            norm = normalize_content(content)
            for m in items:
                if m.get("username") == username and normalize_content(m.get("content", "")) == norm:
                    m["confidence"] = max(float(m.get("confidence") or 0), conf)
                    m["updated_at"] = _now()
                    m["hit"] = int(m.get("hit") or 0) + 1     # 反复提到同一件事 = 更重要
                    return dict(m)
            item = {"id": uuid.uuid4().hex[:8], "username": username, "type": type_,
                    "content": content, "source": (source or "")[:120], "confidence": conf,
                    "enabled": True, "hit": 0, "created_at": _now(), "updated_at": _now()}
            items.append(item)
            trimmed = self._trim(items, username)
            items[:] = trimmed
            return dict(item)

    def update(self, mem_id: str, patch: dict,
               username: str | None = None, is_admin: bool = False) -> dict | None:
        with self._edit_items() as items:
            for m in items:
                if m["id"] == mem_id and (is_admin or m.get("username") == username):
                    if "enabled" in patch:
                        m["enabled"] = bool(patch["enabled"])
                    m["updated_at"] = _now()
                    return dict(m)
        return None

    def delete(self, mem_id: str, username: str | None = None,
               is_admin: bool = False) -> bool:
        with self._edit_items() as items:
            kept = [m for m in items
                    if not (m["id"] == mem_id and (is_admin or m.get("username") == username))]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True

    def clear_user(self, username: str) -> int:
        """删除用户时清理其记忆。"""
        with self._edit_items() as items:
            kept = [m for m in items if m.get("username") != username]
            removed = len(items) - len(kept)
            if removed:
                items[:] = kept
            return removed


memory_store = MemoryStore()
