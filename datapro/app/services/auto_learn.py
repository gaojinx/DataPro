"""自动学习（A11）：成功取数 → 技能规范；对话 → 长期记忆。

设计原则：
- 旁路异步：任何失败只记日志，不影响主链路；
- AI 判断是否值得（worth / remember）+ 置信度阈值，宁可少记不可记错；
- 自动生效、可在技能页撤销；按用户隔离；
- 规范中的 SQL 必须日期占位化，只作生成参考，最终仍走校验/执行。
"""
from __future__ import annotations

import logging

from app.config import settings
from app.llm.client import llm_client
from app.prompts import load_prompt, prompt_version
from app.services.learned_skills import learned_skills
from app.services.memory_store import TYPES, memory_store

logger = logging.getLogger("datapro.auto_learn")

# 记忆抽取的规则预筛：没有这些线索词的用户消息几乎不会包含长期偏好/约定
_MEMORY_HINTS = ("我", "我们", "以后", "默认", "记住", "习惯", "口径", "简称", "别名",
                 "不要", "禁用", "只看", "务必", "统一", "一直", "每次")

AUTO_SYSTEM = (
    "你是 DataPro 的学习评估器，只输出 JSON。"
    "只判断信息是否值得长期保留，不写 SQL、不补充、不编造；"
    "不确定时一律选择不保留。"
)


def _render(name: str, variables: dict[str, str]) -> str:
    """旁路 prompt 渲染：只替换传入变量，不追加检索结果块。"""
    text = load_prompt(name)
    for key, value in variables.items():
        text = text.replace("{" + key + "}", value)
    return text


class AutoLearn:
    async def observe_query(self, *, username: str, question: str, sql: str,
                            caliber: str = "", dimensions: list[str] | None = None) -> dict | None:
        """成功取数后调用：AI 判断是否值得沉淀为技能规范。"""
        try:
            if not settings.auto_learn_enabled or not llm_client.available():
                return None
            question = (question or "").strip()
            if len(question) < 4 or not sql:
                return None
            prompt = _render("auto_skill", {
                "question": question, "sql": sql[:1500],
                "caliber": caliber or "无",
                "dimensions": "、".join(dimensions or []) or "无",
            })
            data = await llm_client.structured_call(
                prompt, system=AUTO_SYSTEM, tool_version=prompt_version("auto_skill"),
                temperature=0.0)
            if not data or not data.get("worth"):
                return None
            confidence = float(data.get("confidence") or 0)
            if confidence < settings.auto_skill_min_confidence:
                return None
            name = " ".join((data.get("name") or "").split())[:40]
            if not name:
                return None
            return learned_skills.upsert(username, {
                "name": name, "question": question,
                "sql_template": data.get("sql_template") or sql,
                "caliber": data.get("caliber") or caliber,
                "notes": data.get("notes") or "",
                "confidence": confidence,
            })
        except Exception as exc:  # noqa: BLE001 —— 旁路失败不影响主链路
            logger.warning("自动沉淀技能规范失败：%s", exc)
            return None

    async def observe_conversation(self, *, username: str, question: str, answer: str,
                                   mode: str = "query") -> dict | None:
        """每轮对话后调用：AI 判断是否包含值得长期记住的信息。"""
        try:
            if not settings.auto_memory_enabled or not llm_client.available():
                return None
            question = (question or "").strip()
            if len(question) < 6 or not any(h in question for h in _MEMORY_HINTS):
                return None
            prompt = _render("auto_memory", {
                "question": question,
                "answer": (answer or "")[:800],
                "mode": mode or "query",
            })
            data = await llm_client.structured_call(
                prompt, system=AUTO_SYSTEM, tool_version=prompt_version("auto_memory"),
                temperature=0.0)
            if not data or not data.get("remember"):
                return None
            confidence = float(data.get("confidence") or 0)
            if confidence < settings.auto_memory_min_confidence:
                return None
            mem_type = data.get("type") if data.get("type") in TYPES else "preference"
            return memory_store.add(username, mem_type, data.get("content") or "",
                                    source=question[:120], confidence=confidence)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动记忆抽取失败：%s", exc)
            return None


auto_learn = AutoLearn()
