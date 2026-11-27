"""生成服务：LLM 生成 SQL（Prompt设计 §3.1 sql_gen），结构化 JSON 输出。

- 有可用 LLM 配置 → 真实生成（B09/B17）；无配置且开启 mock → 骨架降级
- 口径库 logic 与召回表结构注入 prompt，口径优先于模型自身理解
- 超时映射 E005，其余失败返回 None 由状态机重试/升级
"""
import re
from dataclasses import dataclass, field

from app.config import settings
from app.core.errors import DataProError, E005
from app.core.statemachine import PipelineContext
from app.llm.client import LLMNotConfiguredError, LLMTimeoutError, llm_client
from app.prompts import SYSTEM_COMMON, prompt_version, render_prompt
from app.services import fewshot


@dataclass
class Generation:
    sql: str
    explanation: str
    assumptions: list[str] = field(default_factory=list)


# 降级模板的表达式白名单：函数名、列名、算术、括号与比较（SUM(pay_status=2) 需要 =）。
# 不含引号、分号、注释符与子查询关键字——口径库被误填时，也不能借模板把任意 SQL 拼进主链路。
_TEMPLATE_EXPR = re.compile(r"^[A-Za-z0-9_\s(),.*+\-/=]+$")
_TEMPLATE_AGG = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|ROUND)\s*\(", re.IGNORECASE)
# 字符白名单挡不住「SUM(amount) FROM x」这种只由字母空格组成的写法，再挡一层关键字
_TEMPLATE_FORBIDDEN = re.compile(
    r"\b(SELECT|FROM|WHERE|JOIN|UNION|INTO|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|"
    r"GRANT|TRUNCATE|SLEEP|BENCHMARK|AND|OR)\b", re.IGNORECASE)


def _schema_block(ctx: PipelineContext) -> str:
    """召回表结构块：只注入命中表，禁止模型看到全库。

    字段级命中（`hit_columns`）把相关字段排到最前，并单列一行提示：
    宽表有十几个字段时，模型最容易犯的错就是拿「看起来像」的另一个字段凑数。
    """
    lines = []
    for t in ctx.recalled_schemas or []:
        cols = t.get("columns", [])
        labels = t.get("column_labels")
        meta = t.get("column_meta") or []
        hits = [h for h in (t.get("hit_columns") or []) if h in cols]
        if meta and len(meta) == len(cols):
            # 对齐的字段元数据优先：中文名 + 命中字段前置（sorted 稳定，未命中保持原序）
            ordered = sorted(meta, key=lambda m: 0 if m.get("name") in hits else 1)
            col_desc = ", ".join(
                f'{m["name"]}（{m["cn_name"]}）' if m.get("cn_name") else m["name"]
                for m in ordered)
        elif labels and len(labels) == len(cols):
            # 上传文件表：列名为 c1..cn，附原始表头帮助模型理解语义
            col_desc = ", ".join(f"{c}（{lab}）" for c, lab in zip(cols, labels))
        else:
            col_desc = ", ".join(cols)
        lines.append(f"- {t.get('schema', '')}.{t['table_name']}（{t.get('description', '')}）"
                     f" 字段: {col_desc}")
        if hits:
            # 措辞刻意保守：字段命中是「问题里出现的词」推出来的，不等于口径要用的字段，
            # 用「优先使用」这种强指令会在口径与本表字段不一致时把模型带偏
            lines.append(f"  问题中提到的字段（仅供参考，口径仍以其 logic 为准）: {', '.join(hits)}")
        enums = t.get("column_enums") or {}
        if enums:
            # 枚举值：避免模型把“东北”写成“东北区域”这类不存在的取值
            enum_desc = "；".join(f"{col} ∈ {{{val}}}" for col, val in enums.items() if col in cols)
            if enum_desc:
                lines.append(f"  字段取值（筛选时必须原样使用，不得添加“区域/名称”等后缀）：{enum_desc}")
    return "\n".join(lines) or "无"


def _knowledge_block(ctx: PipelineContext) -> str:
    """命中知识库的行业术语：仅作理解参考，口径 logic 优先级更高。"""
    lines = [
        f"- {k['term']}：{k.get('definition', '')}；计算口径：{k.get('formula', '')}"
        for k in ctx.recalled_knowledge or []
    ]
    return "\n".join(lines) or "无"


def _memory_block(ctx: PipelineContext) -> str:
    """A11 用户长期记忆：偏好/术语/规则，仅作参考，与口径冲突时口径优先。"""
    type_labels = {"preference": "偏好", "terminology": "术语", "rule": "规则", "caliber": "口径"}
    lines = [f"- [{type_labels.get(m.get('type'), m.get('type'))}] {m.get('content', '')}"
             for m in (getattr(ctx, "memories", None) or [])[:8]]
    return "\n".join(lines) or "无"


def _learned_block(ctx: PipelineContext) -> str:
    """A11 历史成功规范：同题（归一化后）匹配到才注入，只作结构参考。"""
    try:
        from app.services.learned_skills import learned_skills
        return learned_skills.render_for_prompt(ctx.user_id, ctx.question)
    except Exception:  # noqa: BLE001 —— 学习数据不可用不影响生成
        return "无"


def _caliber_block(ctx: PipelineContext) -> str:
    """相关口径块：logic 优先于模型理解。"""
    lines = [
        f"- {c['metric_name']}（{c.get('metric_id', '')}）：{c.get('definition', '')}；"
        f"logic: {c.get('logic', '')}"
        for c in ctx.recalled_calibers or []
    ]
    block = "\n".join(lines) or "无"
    if lines and getattr(ctx, "forced_caliber_id", None):
        # 口径澄清后用户已选定：生成必须严格按该公式，避免模型回退到另一口径
        block = ("【用户已确认口径，必须严格按下列公式与数据表生成，不得改用其他口径】\n" + block)
    return block


def _time_block(ctx: PipelineContext) -> str:
    """已解析的时间窗：日期由服务端确定，模型只写占位符，不做日期算术。"""
    w = getattr(ctx, "time_window", None)
    if not w:
        return "未指定（执行引擎按默认最近 7 天注入）"
    note = "" if w.explicit else "（用户未指定，系统默认值，必须在 assumptions 中声明）"
    return (f"{w.start.isoformat()} ~ {w.end.isoformat()}（{w.label}）{note}；"
            f"SQL 中写 dt BETWEEN '{{date_start}}' AND '{{date_end}}'")


RETRY_HINTS = {
    1: "上一次未通过校验。请严格按【参考写法】的结构重写，逐条核对字段名与时间占位符。",
    2: "连续失败。请退化为最小可执行查询：单表、单指标、只保留口径必需过滤与时间条件，"
       "不要额外维度、排序或子查询。",
}


class GeneratorService:
    async def generate(self, ctx: PipelineContext) -> Generation | None:
        if not llm_client.available() and settings.llm_mock:
            return self._degraded(ctx)

        # 结构化重试：每一轮都改变输入（示例数量 / 提示语 / 采样温度），不做同义反复
        stage = getattr(ctx, "retry_stage", 0)
        k = 3 if stage >= 1 else 2
        hint = RETRY_HINTS.get(stage, "无")
        if getattr(ctx, "no_sql_count", 0) >= 1:
            hint += " 上一轮未给出 SQL：必须输出 SQL，不确定的写进 assumptions，禁止用 need_info 兜底。"

        # 修正回路（B10）：有上一版 SQL 与校验错误时，走 sql_fix 定向修正而不是从零重写
        if stage >= 1 and ctx.generated_sql and ctx.validation_errors:
            return await self._fix(ctx, ctx.generated_sql, hint)

        prompt = render_prompt("sql_gen", {
            "question": self._question(ctx),
            "spec": ctx.spec.display() if getattr(ctx, "spec", None) else "未抽取",
            "time_window": _time_block(ctx),
            "recalled_schemas": _schema_block(ctx),
            "calibers": _caliber_block(ctx),
            "knowledge": _knowledge_block(ctx),
            "memories": _memory_block(ctx),
            "learned": _learned_block(ctx),
            "examples": fewshot.render(getattr(ctx, "spec", None), ctx.recalled_tables, k),
            "retry_hint": hint,
            "retry_errors": "；".join(ctx.validation_errors) or "无",
        })
        try:
            data = await llm_client.structured_call(
                prompt, system=SYSTEM_COMMON, tool_version=prompt_version("sql_gen"),
                temperature=0.2 if stage == 1 else 0.0)
        except LLMTimeoutError:
            raise DataProError(E005)
        except LLMNotConfiguredError as e:
            raise DataProError(E005, message=str(e))

        # 只有「没有 SQL」才算生成失败；模型自评的 confidence 未经校准，不用于门控
        if not data or not data.get("sql"):
            return None
        return Generation(sql=data["sql"], explanation=data.get("explanation", ""),
                          assumptions=data.get("assumptions", []))

    async def _fix(self, ctx: PipelineContext, sql: str, hint: str) -> Generation | None:
        """定向修正：带上原 SQL 与校验错误，保留查询意图，只修出错的部分。"""
        prompt = render_prompt("sql_fix", {
            "sql": sql,
            "retry_errors": "；".join(ctx.validation_errors) or "未知错误",
            "question": self._question(ctx),
            "spec": ctx.spec.display() if getattr(ctx, "spec", None) else "未抽取",
            "time_window": _time_block(ctx),
            "recalled_schemas": _schema_block(ctx),
            "calibers": _caliber_block(ctx),
            "memories": _memory_block(ctx),
            "learned": _learned_block(ctx),
            "examples": fewshot.render(getattr(ctx, "spec", None), ctx.recalled_tables, 2),
            "retry_hint": hint,
        })
        try:
            data = await llm_client.structured_call(
                prompt, system=SYSTEM_COMMON, tool_version=prompt_version("sql_fix"),
                temperature=0.2)
        except LLMTimeoutError:
            raise DataProError(E005)
        except LLMNotConfiguredError as e:
            raise DataProError(E005, message=str(e))
        if not data or not data.get("sql"):
            return None
        return Generation(sql=data["sql"],
                          explanation=data.get("explanation") or "根据校验错误修正后的查询",
                          assumptions=data.get("assumptions", []))

    @staticmethod
    def _question(ctx: PipelineContext) -> str:
        """原始问题（含多轮上下文）始终保留，IR 作为独立规格注入，二者不互相替代。

        IR 负责锁定指标/时间/维度，原句负责保留业务语义（"有多少…注册过"这类限定），
        只给 IR 会让模型丢掉原句里的修饰，反而更容易生成 need_info。
        """
        return GeneratorService._question_with_history(ctx)

    @staticmethod
    def _question_with_history(ctx: PipelineContext) -> str:
        """多轮上下文：只带此前的取数类提问，避免"你是谁"等寒暄污染生成。"""
        data_words = ("订单", "用户", "销量", "销售额", "活跃", "数量", "金额", "率", "GMV")
        prevs = [m.get("content", "") for m in ctx.history if m.get("role") == "user"][:-1]
        prevs = [u for u in prevs if any(w in u for w in data_words)][-2:]
        base = ctx.question
        if prevs:
            block = "\n".join(f"- {u}" for u in prevs)
            base = f"{ctx.question}\n\n（此前提问，仅作指代理解）：\n{block}"
        # 上传图片的文字描述：模型看不到图，只能靠这段文字理解"图里有什么"。
        # 明确标注为「仅供参考」——图片里的数字不是数仓数据，不能拿它当口径或结果。
        if getattr(ctx, "image_notes", ""):
            base += ("\n\n【上传图片内容（视觉模型识别，仅作上下文理解，"
                     "不是数仓数据，不得当作查询结果）】\n" + ctx.image_notes)
        return base

    @staticmethod
    @staticmethod
    def _template_from_caliber(c: dict, ctx: PipelineContext) -> Generation | None:
        """把一个口径的 logic 模板化成可执行 SQL；不适用时返回 None。

        口径 logic 的形态是 `<聚合表达式> WHERE <过滤>`，聚合部分有 COUNT / SUM /
        ROUND(...) / 比值等；只认 COUNT 会让「销售额、客单价、退款金额、广告 ROI」
        这些最常见的指标在无模型时全部转人工。
        """
        logic = (c.get("logic") or "").strip()
        table = c.get("table") or (ctx.recalled_tables[0] if ctx.recalled_tables else None)
        if not table or not logic:
            return None
        parts = re.split(r"\s+WHERE\s+", logic, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            return None
        expr, where = parts[0].strip(), parts[1].strip()
        # 表达式只允许函数、列名、算术与括号：口径库被误填时不能借模板把任意 SQL 拼进来
        if (not _TEMPLATE_EXPR.match(expr) or not _TEMPLATE_AGG.search(expr)
                or _TEMPLATE_FORBIDDEN.search(expr)):
            return None
        # 只认识这三种日期占位符；出现别的（如 {month_start}）说明口径写法超出模板能力
        def _unknown_placeholder(text: str) -> bool:
            return "{" in re.sub(r"\{(?:date|date_start|date_end)\}", "", text)

        if _unknown_placeholder(where):
            return None                     # 有未知占位符，宁可转人工也不发错 SQL
        # 口径 logic 里的 {date} 是单日示意；降级时若照抄，多日窗口只会查第一天，
        # 用户看到的是 7 天窗口却得到 1 天的数（静默错数）。统一改写成区间。
        # 注意日期列不一定是 dt：新增用户（注册口径）用的是 register_date，
        # 只重写 dt 会让 SQL 里留下字面量 '{date}'，执行必定查不到数据。
        where = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'\{date\}'",
                       r"\1 BETWEEN '{date_start}' AND '{date_end}'", where)
        if "{date}" in where:
            return None                     # 还有没被重写的单日占位符：不发这条 SQL
        alias = (c.get("metric_id") or "metric").lower()
        window_note = ctx.time_window.text() if ctx.time_window else "未指定（默认最近 7 天）"
        return Generation(
            sql=f"SELECT {expr} AS {alias} FROM {table} WHERE {where} LIMIT 200",
            explanation=f"按口径 {c.get('metric_id')}（{c['metric_name']}）模板生成",
            assumptions=[
                f"未配置 LLM API，使用口径库 {c.get('metric_id')} 模板生成",
                f"时间范围：{window_note}",
            ],
        )

    @staticmethod
    def _degraded(ctx: PipelineContext) -> Generation | None:
        """无可用 LLM 时的降级：

        - 命中口径库 → 用口径模板（人工维护的可信源）组装查询，结果与业务口径一致
        - 未命中口径（或口径 logic 无法模板化）→ 返回 None 走升级转人工，绝不返回示例数据

        按召回顺序逐个尝试：第一个口径的 logic 模板化失败（缺 WHERE、含未知占位符）时，
        后面命中的口径仍有机会，而不是直接转人工。
        """
        for c in ctx.recalled_calibers or []:
            gen = GeneratorService._template_from_caliber(c, ctx)
            if gen is not None:
                return gen
        return None


class MockGenerator:
    """显式 mock（单测/评测用），行为与降级一致。"""

    async def generate(self, ctx: PipelineContext) -> Generation | None:
        return GeneratorService._degraded(ctx)


generator_service = GeneratorService()
