"""技能规范（A11 自动沉淀）：成功取数后由 AI 判断值得复用而写入的「标准问法 + 占位化 SQL」。

- 按用户隔离（每个人只复用自己沉淀的规范），JSON 持久化 `data/learned_skills.json`
- 归一化问题去重：同一类问题（只是时间不同）只保留一条，命中次数累加
- 单人上限 `MAX_PER_USER`；SQL 模板必须完成日期占位化，否则只保留文字规范
- 规范只作为生成参考，最终 SQL 仍走校验 / 执行（不直接执行历史 SQL）
- 多实例加固：mtime 缓存失效 + 跨进程锁内 read-modify-write，读路径返回副本
"""
from __future__ import annotations

import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.services.shared_json import SharedJson

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "learned_skills.json"
MAX_PER_USER = 30
MAX_SQL = 2000

_PUNCT = re.compile(r"[\s，。！？、,.!?：:；;\"'“”‘’()（）\[\]【】<>《》]+")
_TIME_WORDS = ("大前天", "前天", "昨天", "昨日", "今天", "今日", "当天",
               "本周", "这周", "本星期", "这个星期", "上周", "上个星期", "上星期",
               "本月", "这个月", "上月", "上个月", "本季度", "这个季度", "上季度", "上个季度",
               "今年", "本年", "去年", "最近", "近", "过去")
_NUM = re.compile(r"\d+")


def normalize_question(question: str) -> str:
    """去掉时间表达与标点数字，用于判断「是不是同一类问题」。"""
    s = (question or "").lower()
    s = _PUNCT.sub("", s)
    s = re.sub(r"(最近|近|过去)\d+(天|日|周|个?星期|个?月|个?季度|年)", "", s)
    s = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?", "", s)
    s = re.sub(r"\d{4}年", "", s)
    s = re.sub(r"\d{1,2}月(\d{1,2}[日号]?)?", "", s)
    for w in _TIME_WORDS:
        s = s.replace(w, "")
    # 去掉常见问法动词与助词，让“查昨天的支付订单数”“2026-11-20 支付订单数”归一化一致
    for w in ("查询", "查一下", "查", "看一下", "看看", "看", "统计", "是多少", "有多少", "的"):
        s = s.replace(w, "")
    return _NUM.sub("", s)


def placeholderize(sql: str) -> str:
    """把具体日期替换成服务端占位符；替换不干净就放弃模板（宁可只留文字规范）。"""
    if not sql:
        return ""
    s = re.sub(r"BETWEEN\s*'[^']+'\s*AND\s*'[^']+'",
               "BETWEEN '{date_start}' AND '{date_end}'", sql, flags=re.IGNORECASE)
    s = re.sub(r"'\d{4}-\d{2}-\d{2}'", "'{date_start}'", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "{date_start}", s)
    if re.search(r"\d{4}-\d{2}-\d{2}", s):
        return ""
    return s.strip().rstrip(";")[:MAX_SQL]


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} or {text}


def _overlap(a: str, b: str) -> float:
    left, right = _bigrams(a), _bigrams(b)
    return len(left & right) / max(1, len(left | right))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class LearnedSkills:
    def __init__(self, path: Path | None = None):
        self._path = path or DATA_FILE
        self._cache: list[dict] | None = None  # 兼容旧测试 patch；实际缓存由 SharedJson 管理

    def _store(self) -> SharedJson:
        return SharedJson(self._path, default=[])

    @contextmanager
    def _edit_items(self):
        """写入时返回可变的技能列表，兼容历史 {"skills": [...]} 包装格式。

        与 memory_store 同一处历史遗留：读路径已兼容包装，写路径没有，导致
        新增/停用/删除技能全部抛 'str' object has no attribute 'get'。
        """
        with self._store().edit() as doc:
            if isinstance(doc, dict):
                yield doc.setdefault("skills", [])
            else:
                yield doc

    def _load(self) -> list[dict]:
        data = self._store().snapshot()
        if isinstance(data, dict):
            data = data.get("skills", [])
        return list(data) if isinstance(data, list) else []

    # ---- 查询 ----
    def list(self, username: str, all_users: bool = False) -> list[dict]:
        items = [dict(s) for s in self._load()]
        if not all_users:
            items = [s for s in items if s.get("username") == username]
        return sorted(items, key=lambda s: s.get("updated_at") or "", reverse=True)

    def match(self, username: str, question: str) -> dict | None:
        norm = normalize_question(question)
        if not norm:
            return None
        items = [s for s in self._load()
                 if s.get("username") == username and s.get("enabled", True)]
        if not items:
            return None
        for s in items:
            if s.get("norm") == norm:
                return dict(s)
        best, best_score = None, 0.0
        for s in items:
            score = _overlap(norm, s.get("norm", ""))
            if score > best_score:
                best, best_score = s, score
        return dict(best) if best is not None and best_score >= 0.6 else None

    def render_for_prompt(self, username: str, question: str) -> str:
        hit = self.match(username, question)
        if not hit:
            return "无"
        lines = [f"- 规范：{hit.get('name', '')}；标准问法：{hit.get('question', '')}"]
        if hit.get("caliber"):
            lines.append(f"  口径：{hit['caliber']}")
        if hit.get("sql_template"):
            lines.append(f"  历史成功 SQL（日期已占位，字段以可用表结构为准）：{hit['sql_template']}")
        if hit.get("notes"):
            lines.append(f"  注意：{hit['notes']}")
        return "\n".join(lines)

    @staticmethod
    def _trim(items: list[dict], username: str) -> list[dict]:
        mine = [s for s in items if s.get("username") == username]
        if len(mine) <= MAX_PER_USER:
            return items
        mine.sort(key=lambda s: (bool(s.get("enabled", True)),
                                 int(s.get("hit") or 0),
                                 s.get("updated_at") or ""))
        drop = {s["id"] for s in mine[:len(mine) - MAX_PER_USER]}
        return [s for s in items if s.get("id") not in drop]

    # ---- 写入 ----
    def upsert(self, username: str, payload: dict) -> dict | None:
        username = (username or "").strip()
        name = " ".join((payload.get("name") or "").split())[:40]
        question = " ".join((payload.get("question") or "").split())[:120]
        norm = normalize_question(question)
        if not username or not name or not norm:
            return None
        sql_template = placeholderize(payload.get("sql_template") or "")
        caliber = (payload.get("caliber") or "").strip()[:80]
        notes = " ".join((payload.get("notes") or "").split())[:160]
        confidence = round(float(payload.get("confidence") or 0), 2)

        with self._edit_items() as items:
            for s in items:
                if s.get("username") == username and s.get("norm") == norm:
                    s["name"] = name
                    s["question"] = question
                    if sql_template:
                        s["sql_template"] = sql_template
                    if caliber:
                        s["caliber"] = caliber
                    if notes:
                        s["notes"] = notes
                    s["confidence"] = max(float(s.get("confidence") or 0), confidence)
                    s["hit"] = int(s.get("hit") or 0) + 1
                    s["updated_at"] = _now()
                    return dict(s)
            item = {"id": uuid.uuid4().hex[:8], "username": username, "name": name,
                    "question": question, "norm": norm, "sql_template": sql_template,
                    "caliber": caliber, "notes": notes, "confidence": confidence,
                    "auto": True, "enabled": True, "hit": 1,
                    "created_at": _now(), "updated_at": _now()}
            items.append(item)
            trimmed = self._trim(items, username)
            items[:] = trimmed
            return dict(item)

    def update(self, skill_id: str, patch: dict,
               username: str | None = None, is_admin: bool = False) -> dict | None:
        with self._edit_items() as items:
            for s in items:
                if s["id"] == skill_id and (is_admin or s.get("username") == username):
                    if "enabled" in patch:
                        s["enabled"] = bool(patch["enabled"])
                    s["updated_at"] = _now()
                    return dict(s)
        return None

    def delete(self, skill_id: str, username: str | None = None,
               is_admin: bool = False) -> bool:
        with self._edit_items() as items:
            kept = [s for s in items
                    if not (s["id"] == skill_id and (is_admin or s.get("username") == username))]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True

    def clear_user(self, username: str) -> int:
        with self._edit_items() as items:
            kept = [s for s in items if s.get("username") != username]
            removed = len(items) - len(kept)
            if removed:
                items[:] = kept
            return removed


learned_skills = LearnedSkills()
