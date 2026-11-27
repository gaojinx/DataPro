"""LLM 意图识别（第 3 项）：模型判断取数 / 口径问答 / 寒暄 / 越界，失败回退规则。

主链路只接受三类覆盖：query、meta_qa、reject（映射为 forbidden）。
- smalltalk 不在流水线内处理（网关层已用规则分流寒暄），因此忽略
- 规则判定为 forbidden（敏感/写操作/越界）时一律保持，不被模型覆盖
"""
import json
import logging

from app.config import settings
from app.llm.client import llm_client
from app.services.metrics import metrics
from app.services.query_cache import SCOPE_INTENT, fingerprint, normalize, query_cache

logger = logging.getLogger("datapro.intent_llm")

SYSTEM = (
    "你是数据管家的意图分类器。把用户问题归为四类之一，只输出 JSON："
    '{"intent":"query|meta_qa|smalltalk|reject","reason":"不超过20字"}。'
    "query=要查数据（含时间/指标/维度）；"
    "meta_qa=问口径、指标定义、表结构、字段含义或数据范围；"
    "smalltalk=寒暄、身份、能力、与数据无关的闲聊；"
    "reject=写操作、导出敏感数据、越权访问系统表或明显违规请求。"
    "拿不准时优先 query。不要输出 JSON 之外的任何内容。"
)

INTENTS = {"query", "meta_qa", "smalltalk", "reject"}


async def classify(question: str, history: list[dict] | None = None) -> str | None:
    """返回模型判定意图；未启用/无模型/解析失败返回 None（调用方回退规则）。"""
    question = (question or "").strip()
    if settings.llm_mock or not settings.intent_llm_enabled or not question:
        return None
    recent = [f"用户：{m.get('content', '')}" for m in (history or [])[-3:]
              if m.get("role") == "user" and m.get("content")]
    prompt = ("【最近提问】\n" + "\n".join(recent) + "\n\n" if recent else "") + \
             f"【当前问题】\n{question}"

    # 限定缓存（U-B03）：分类结果是「问题 + 最近几轮提问」的纯函数，可安全复用
    cache_key = fingerprint(normalize(question), *[normalize(r) for r in recent])
    cached = await query_cache.get(SCOPE_INTENT, cache_key)
    if cached and cached.get("intent") in INTENTS:
        metrics.inc("datapro_cache_total", scope=SCOPE_INTENT, result="hit")
        return cached["intent"]
    metrics.inc("datapro_cache_total", scope=SCOPE_INTENT, result="miss")

    try:
        text = await llm_client.chat(prompt, system=SYSTEM)
        if not text:
            metrics.inc("datapro_intent_llm_total", result="empty")
            return None
        payload = text.strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            payload = payload.split("\n", 1)[-1] if "\n" in payload else payload
        intent = json.loads(payload).get("intent")
        if intent in INTENTS:
            metrics.inc("datapro_intent_llm_total", result="ok", intent=intent)
            await query_cache.set(SCOPE_INTENT, cache_key, {"intent": intent},
                                  settings.cache_ttl_intent)
            return intent
    except Exception as exc:  # noqa: BLE001 —— 分类失败不影响主链路
        logger.info("LLM 意图识别失败，回退规则：%s", exc)
    metrics.inc("datapro_intent_llm_total", result="fallback")
    return None
