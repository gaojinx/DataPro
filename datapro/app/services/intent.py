"""意图识别与澄清（状态机 INTENT/CLARIFY 节点）。

骨架用关键词规则；接 LLM 后替换为 intent.txt / clarify.txt 驱动的结构化分类。
"""
from app.core.statemachine import PipelineContext, FORBIDDEN_PATTERNS, META_QA_PATTERNS
from app.llm.client import llm_client
from app.prompts import render_prompt


class IntentService:
    async def classify(self, ctx: PipelineContext) -> str:
        q = ctx.question.lower()
        if any(p in q for p in FORBIDDEN_PATTERNS):
            return "forbidden"
        if any(p in q for p in META_QA_PATTERNS):
            return "meta_qa"
        return "query"

    async def check_sufficiency(self, ctx: PipelineContext) -> bool:
        """三要素齐全才放行：时间范围 + 指标口径 + 输出粒度（Prompt设计 §3.2）。

        骨架规则版；接 LLM 后由 clarify.txt 判定 sufficient。
        """
        q = ctx.question
        has_time = any(w in q for w in ("昨天", "今天", "上周", "本周", "上月", "本月", "最近", "天", "月", "周", "年", "日"))
        has_metric = any(w in q for w in ("订单", "用户", "销售额", "销量", "活跃", "数量", "数", "率", "金额", "GMV", "gmv"))
        return has_time and has_metric

    async def ask_clarify(self, ctx: PipelineContext) -> str:
        # 必须给选项，禁止开放式反问
        return "请补充信息：① 时间范围（如：昨天/最近7天）② 指标口径（如：支付订单数还是全部订单）③ 输出粒度（明细还是汇总）"


intent_service = IntentService()


class AnswerService:
    """PRESENT / 元数据问答节点。骨架返回模板话术；接 LLM 后走 answer.txt / meta_qa.txt。"""

    AI_DISCLAIMER = "以上为 AI 生成结果，重要决策请人工复核。"

    async def summarize(self, ctx: PipelineContext) -> str:
        r = ctx.result
        if not r:
            return self.AI_DISCLAIMER
        trend = f"返回 {r['row_count']} 行" + ("（已截断，可申请导出）" if r.get("truncated") else "")
        return f"查询完成，{trend}。{self.AI_DISCLAIMER}"

    async def answer_meta(self, ctx: PipelineContext, recall) -> str:
        parts = []
        for c in recall.calibers:
            parts.append(f"【{c['metric_name']}】{c['definition']}；计算口径：{c['logic']}")
        for t in recall.tables:
            parts.append(f"【{t['table_name']}】{t['description']}")
        if not parts:
            return "未找到相关口径，请联系数据团队补充口径库。"
        return "\n".join(parts) + "\n" + self.AI_DISCLAIMER


answer_service = AnswerService()
