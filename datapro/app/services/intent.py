"""意图识别与澄清（状态机 INTENT/CLARIFY 节点）。

骨架用关键词规则；接 LLM 后替换为 intent.txt / clarify.txt 驱动的结构化分类。
"""
import json
import logging
import uuid

from app.config import settings
from app.core.statemachine import PipelineContext, FORBIDDEN_PATTERNS, META_QA_PATTERNS
from app.llm.client import llm_client
from app.prompts import load_prompt, render_prompt
from app.services.metrics import metrics
from app.services.query_cache import SCOPE_META, fingerprint, normalize, query_cache

logger = logging.getLogger("datapro.answer")


def _visible_tables(username: str) -> str:
    """可见表白名单快照：`None`（继承全局）与「已配置但为空」必须区分，否则会串味。

    取快照失败时返回一次性随机值，等于强制不命中——宁可少一次缓存，不可错一次答案。
    """
    try:
        from app.services.db_access import db_access

        tables = db_access.tables_for(username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("取可见表白名单失败，本轮跳过缓存：%s", exc)
        return f"unknown:{uuid.uuid4().hex}"
    if tables is None:
        return "inherit"
    return ",".join(sorted(tables))

META_SYSTEM = (
    "你是 DataPro 数据管家，面向业务人员的数据问答助手。"
    "只能依据【参考资料】回答，不得编造资料之外的指标、数值或结论。"
    "用简洁中文：先一句话给定义，再给计算口径（资料提供时），最后可补一句提问示例。"
    "不要输出 Markdown 表格，不要使用编号列表。"
)

SUMMARY_SYSTEM = (
    "你是 DataPro 数据管家。请用中文一句话总结查询结果，不超过 60 字。"
    "只能使用【查询结果】里出现的数值，不得新增、推算或估计任何数字；"
    "聚合结果直接给出该数值及单位口径，明细结果说明分布要点。"
    "不要输出 Markdown，不要出现“结果需人工复核”这类话。"
)

SCOPE_SYSTEM = (
    "你是 DataPro 数据管家，面向业务人员的取数助手。"
    "用户的问题没有精确命中数仓现有的表或指标口径，但不要直接拒答，更不要只说“无法回答”。"
    "请用简洁中文按顺序回答："
    "① 先判断最可能的意图，并直接给出该意图下的答案——能落到现有表或口径的，"
    "说明用哪张表、哪个口径、能查到什么数据；落不到的，直接说清这一点。"
    "② 再列出其他可能的理解（最多 2 条），每条用一句话给出对应答案或说明。"
    "③ 最后用一句话说明当前可查询的表与指标范围。"
    "若用户问的是制度、规范、流程类资料而【可查询范围】里没有，说明这类资料需要先上传到文档知识库，不要凭空作答。"
    "严禁编造任何数值、数据结论以及不存在的表或指标；不要输出 Markdown 表格。"
)


def _load_doc_system() -> str:
    """文档 RAG 的 system prompt：走 prompts/doc_qa.txt，纳入版本管理。

    文件缺失时回退 META_SYSTEM 而不是让导入失败——少一个 prompt 不该让后端起不来。
    """
    try:
        return load_prompt("doc_qa")
    except FileNotFoundError:
        logger.warning("prompts/doc_qa.txt 缺失，文档问答回退通用口径提示词")
        return META_SYSTEM


SYSTEM_DOC = _load_doc_system()


class IntentService:
    async def classify(self, ctx: PipelineContext) -> str:
        rule = self._rule(ctx.question)
        # 规则已判越界/口径问答时不再花一次模型调用；只有“规则=取数”才请模型复核
        if rule != "query":
            return rule
        from app.services.intent_llm import classify as classify_llm

        llm_intent = await classify_llm(getattr(ctx, "raw_question", None) or ctx.question,
                                        getattr(ctx, "history", None))
        if llm_intent == "reject":
            return "forbidden"
        if llm_intent == "meta_qa":
            return "meta_qa"
        return rule

    @staticmethod
    def _rule(question: str) -> str:
        q = (question or "").lower()
        if any(p in q for p in FORBIDDEN_PATTERNS):
            return "forbidden"
        # 数据边界（「数据最新到几号」「数据覆盖哪些日期」）：按元数据问答处理，
        # 避免被模型判成越界；状态机会直接读真实分区回答
        from app.services.data_boundary import detect as is_boundary
        if is_boundary(question):
            return "meta_qa"
        if any(p in q for p in META_QA_PATTERNS):
            return "meta_qa"
        return "query"

    async def check_sufficiency(self, ctx: PipelineContext) -> bool:
        """放行条件：时间窗已确定 + 指标可识别（指标优先取 IR，其次原句关键词）。"""
        if getattr(ctx, "time_unresolved", False):
            return False
        spec = getattr(ctx, "spec", None)
        if spec is not None and "time" in (spec.ambiguous or []) and ctx.time_window is None:
            return False
        if spec is not None and spec.metric:
            return True
        return self._has_metric(ctx.question)

    @staticmethod
    def _has_metric(q: str) -> bool:
        return any(w in q for w in ("订单", "用户", "销售额", "销量", "活跃", "数量", "数", "率", "金额", "GMV", "gmv"))

    async def ask_clarify(self, ctx: PipelineContext) -> str:
        # 必须给选项，禁止开放式反问；只问缺失/歧义要素，一次问完
        q = ctx.question
        spec = getattr(ctx, "spec", None)
        missing = []
        if getattr(ctx, "time_unresolved", False):
            if any(k in q for k in ("同比", "环比", "对比", "相比")):
                missing.append("要对比的两个时间段（如：本月 vs 上月）")
            else:
                missing.append("时间范围（如：昨天 / 最近 7 天 / 上月）")
        elif spec is not None and "time" in (spec.ambiguous or []) and ctx.time_window is None:
            missing.append("时间范围（如：昨天 / 最近 7 天 / 上月）")
        if not (spec is not None and spec.metric) and not self._has_metric(q):
            missing.append("指标口径（如：支付订单数 / 日活跃用户数）")
        if not missing:
            missing.append("输出粒度（汇总还是明细）")
        return "请补充：" + "；".join(missing)


intent_service = IntentService()


class AnswerService:
    """PRESENT / 元数据问答节点：以 AI 理解 + 知识库资料组织答案，模板仅作降级。"""

    async def summarize(self, ctx: PipelineContext) -> str:
        r = ctx.result
        if not r:
            return "暂无查询结果。"
        fallback = f"查询完成，返回 {r['row_count']} 行" + ("（已截断，可申请导出）" if r.get("truncated") else "") + "。"
        payload = {
            "问题": ctx.question,
            "时间窗": ctx.time_window.text() if getattr(ctx, "time_window", None) else None,
            "列": r.get("columns", []),
            "行": (r.get("rows") or [])[:20],
            "总行数": r.get("row_count"),
            "是否截断": bool(r.get("truncated")),
            "生成说明": r.get("explanation", ""),
            "数据自检": getattr(ctx, "self_check_notes", []),
            "口径": [{"名称": c.get("metric_name"), "定义": c.get("definition", "")}
                     for c in ctx.recalled_calibers],
        }
        try:
            text = await llm_client.chat(
                "【查询结果】\n" + json.dumps(payload, ensure_ascii=False, default=str), system=SUMMARY_SYSTEM)
            if text:
                return text
        except Exception as exc:  # noqa: BLE001 —— 摘要失败不影响结果返回
            logger.warning("结果摘要调用模型失败，回退模板：%s", exc)
        return fallback

    async def explain_scope(self, ctx: PipelineContext, recall) -> str | None:
        """召回 0 命中时的范围说明：由模型说明可查询范围与替代问法，不编造数值。

        无可用模型时返回 None，由状态机按 E002 拒答。
        """
        from app.services.metadata_store import metadata_store
        if not llm_client.available():
            return None
        catalog = "可查询表：\n" + "\n".join(
            f"- {t['schema']}.{t['table_name']}：{t['description']}" for t in metadata_store.tables())
        catalog += "\n可查询指标口径：" + "、".join(c["metric_name"] for c in metadata_store.calibers())
        # 制度/规范类问题没有「表」可查，但补文档后就能查——不写这句，用户只会认为系统能力不足
        catalog += ("\n说明：制度、规范、流程类资料不在上述表中，"
                    "可由管理员在「知识库 → 文档知识库」上传后检索。")
        try:
            return await llm_client.chat(
                f"【可查询范围】\n{catalog}\n\n【用户问题】{ctx.question}", system=SCOPE_SYSTEM)
        except Exception as exc:  # noqa: BLE001
            logger.warning("范围说明调用模型失败，回退拒答：%s", exc)
            return None

    async def answer_meta(self, ctx: PipelineContext, recall,
                          docs: list[dict] | None = None) -> str:
        """口径/元数据问答；传了 `docs` 时把文档 RAG 命中一并作为参考资料。

        `docs` 为空是常态（大多数问题不需要翻文档），此时行为与引入 RAG 之前完全一致，
        走 META_SYSTEM 与原缓存键，不给存量链路引入回归。
        """
        from app.services.doc_rag import doc_rag

        parts = []
        for k in getattr(recall, "knowledge", []):
            parts.append(f"【{k['term']}】定义：{k['definition']}；计算口径：{k['formula']}")
        for c in recall.calibers:
            parts.append(f"【{c['metric_name']}】定义：{c['definition']}；计算口径：{c['logic']}")
        for t in recall.tables:
            parts.append(f"【{t['table_name']}】{t['description']}")
        doc_block = doc_rag.render_context(docs) if docs else ""
        if not parts and not doc_block:
            return "未找到相关口径，请联系数据团队补充口径库。"
        context = "\n".join(parts)
        # 限定缓存（U-B03）：键含资料指纹 + 身份 + 可见表白名单。
        # 文档资料也必须进指纹：否则新增/更新文档后，同样的问题仍返回旧答案。
        cache_key = fingerprint(normalize(ctx.question), context + "\n" + doc_block,
                                ctx.user_id, ctx.user_role, _visible_tables(ctx.user_id))
        cached = await query_cache.get(SCOPE_META, cache_key)
        if cached and cached.get("answer"):
            metrics.inc("datapro_cache_total", scope=SCOPE_META, result="hit")
            return cached["answer"]
        metrics.inc("datapro_cache_total", scope=SCOPE_META, result="miss")
        system = SYSTEM_DOC if doc_block else META_SYSTEM
        prompt = f"【参考资料】\n{context or '无'}"
        if doc_block:
            prompt += f"\n\n【文档资料】\n{doc_block}"
        prompt += f"\n\n【用户问题】{ctx.question}"
        try:
            text = await llm_client.chat(prompt, system=system)
            if text:
                await query_cache.set(SCOPE_META, cache_key, {"answer": text},
                                      settings.cache_ttl_meta)
                return text
        except Exception as exc:  # noqa: BLE001 —— 无模型时降级为资料原文
            logger.warning("口径问答调用模型失败，回退资料原文：%s", exc)
        # 降级为资料原文：本地拼接、零成本，不进缓存（避免把降级结果固化）。
        # 有文档命中时把带编号的片段一并返回，用户至少能看到依据出自哪一段。
        return "\n\n".join(p for p in (context, doc_block) if p)


answer_service = AnswerService()
