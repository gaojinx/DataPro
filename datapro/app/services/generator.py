"""生成服务：LLM 生成 SQL（Prompt设计 §3.1 sql_gen），结构化 JSON 输出。

- 有可用 LLM 配置 → 真实生成（B09/B17）；无配置且开启 mock → 骨架降级
- 口径库 logic 与召回表结构注入 prompt，口径优先于模型自身理解
- 超时映射 E005，其余失败返回 None 由状态机重试/升级
"""
from dataclasses import dataclass, field

from app.config import settings
from app.core.errors import DataProError, E005
from app.core.statemachine import PipelineContext
from app.llm.client import LLMNotConfiguredError, LLMTimeoutError, llm_client
from app.prompts import SYSTEM_COMMON, prompt_version, render_prompt


@dataclass
class Generation:
    sql: str
    explanation: str
    assumptions: list[str] = field(default_factory=list)


def _schema_block(ctx: PipelineContext) -> str:
    """召回表结构块：只注入命中表，禁止模型看到全库。"""
    lines = []
    for t in ctx.recalled_schemas or []:
        cols = t.get("columns", [])
        labels = t.get("column_labels")
        if labels and len(labels) == len(cols):
            # 上传文件表：列名为 c1..cn，附原始表头帮助模型理解语义
            col_desc = ", ".join(f"{c}（{lab}）" for c, lab in zip(cols, labels))
        else:
            col_desc = ", ".join(cols)
        lines.append(f"- {t.get('schema', '')}.{t['table_name']}（{t.get('description', '')}）"
                     f" 字段: {col_desc}")
    return "\n".join(lines) or "无"


def _caliber_block(ctx: PipelineContext) -> str:
    """相关口径块：logic 优先于模型理解。"""
    lines = [
        f"- {c['metric_name']}（{c.get('metric_id', '')}）：{c.get('definition', '')}；"
        f"logic: {c.get('logic', '')}"
        for c in ctx.recalled_calibers or []
    ]
    return "\n".join(lines) or "无"


class GeneratorService:
    async def generate(self, ctx: PipelineContext) -> Generation | None:
        if not llm_client.available() and settings.llm_mock:
            return self._degraded(ctx)

        prompt = render_prompt("sql_gen", {
            "question": self._question_with_history(ctx),
            "recalled_schemas": _schema_block(ctx),
            "calibers": _caliber_block(ctx),
            "retry_errors": "；".join(ctx.validation_errors) or "无",
        })
        try:
            data = await llm_client.structured_call(
                prompt, system=SYSTEM_COMMON, tool_version=prompt_version("sql_gen"))
        except LLMTimeoutError:
            raise DataProError(E005)
        except LLMNotConfiguredError as e:
            raise DataProError(E005, message=str(e))

        if not data or data.get("confidence") == "low" or not data.get("sql"):
            return None
        return Generation(sql=data["sql"], explanation=data.get("explanation", ""),
                          assumptions=data.get("assumptions", []))

    @staticmethod
    def _question_with_history(ctx: PipelineContext) -> str:
        """多轮追问：拼最近 2 轮用户问答，保留上下文。"""
        recent = [m for m in ctx.history if m.get("role") == "user"][-2:]
        if len(recent) <= 1:
            return ctx.question
        return "\n".join(f"- {m['content']}" for m in recent)

    @staticmethod
    def _degraded(ctx: PipelineContext) -> Generation | None:
        """无可用 LLM 时的降级：

        - 命中口径库 → 用口径模板（人工维护的可信源）组装查询，结果与业务口径一致
        - 未命中口径 → 返回 None 走升级转人工，绝不返回无关示例数据
        """
        if ctx.recalled_calibers:
            c = ctx.recalled_calibers[0]
            logic = (c.get("logic") or "").strip()
            table = c.get("table") or (ctx.recalled_tables[0] if ctx.recalled_tables else None)
            if table and logic.upper().startswith("COUNT") and " WHERE " in logic:
                select_part, where_part = logic.split(" WHERE ", 1)
                alias = (c.get("metric_id") or "metric").lower()
                return Generation(
                    sql=f"SELECT {select_part} AS {alias} FROM {table} "
                        f"WHERE {where_part} LIMIT 200",
                    explanation=f"按口径 {c.get('metric_id')}（{c['metric_name']}）模板生成",
                    assumptions=[
                        f"未配置 LLM API，使用口径库 {c.get('metric_id')} 模板生成",
                        "时间范围默认取昨日分区",
                    ],
                )
        return None


class MockGenerator:
    """显式 mock（单测/评测用），行为与降级一致。"""

    async def generate(self, ctx: PipelineContext) -> Generation | None:
        return GeneratorService._degraded(ctx)


generator_service = GeneratorService()
