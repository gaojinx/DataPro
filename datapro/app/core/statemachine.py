"""取数状态机（HLD §4 核心）。

节点流转：
INTENT → (取数) CLARIFY → RECALL → GENERATE → VALIDATE → EXECUTE → PRESENT
       → (问答) RECALL → PRESENT
       → (越界/无口径/模糊) REJECT
       → (生成/执行反复失败) ESCALATE
设计约束（Prompt设计 §3.1 / 接口规范 §2.3）：
- VALIDATE 不过：带错误回 GENERATE 修正，最多 2 次，仍不过 → REJECT
- CLARIFY 最多 2 轮，仍模糊 → REJECT
- RECALL 0 命中 → 直接 REJECT，禁止模型自由发挥
"""
import enum
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from app.config import settings
from app.core.errors import DataProError, E001, E002, E003
from app.core.trace import current_trace_id


class Node(str, enum.Enum):
    INTENT = "INTENT"
    CLARIFY = "CLARIFY"
    RECALL = "RECALL"
    GENERATE = "GENERATE"
    VALIDATE = "VALIDATE"
    EXECUTE = "EXECUTE"
    PRESENT = "PRESENT"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


MAX_CLARIFY_ROUNDS = 2
MAX_GENERATE_RETRIES = 2

# 越界关键词（骨架用规则实现意图分类，接 LLM 后替换为 prompt 意图识别）
FORBIDDEN_PATTERNS = ("update", "delete", "drop", "insert", "truncate", "改", "删", "写入", "导入", "修改",
                      "导出", "下载", "dump", "export", "手机号", "身份证", "银行卡", "密码")
META_QA_PATTERNS = ("口径", "什么意思", "怎么算", "怎么定义", "字段含义", "有哪些值",
                    "干嘛用", "干什么用", "做什么用", "是做什么", "用途", "表结构", "字段有哪些")


@dataclass
class PipelineContext:
    """一次取数请求的全量上下文。"""
    question: str
    user_id: str
    session_id: str = ""
    history: list[dict] = field(default_factory=list)
    clarify_count: int = 0
    generate_retries: int = 0
    recalled_tables: list[str] = field(default_factory=list)  # 白名单凭据：只有这里出现过的表允许出现在 SQL 中
    recalled_schemas: list[dict] = field(default_factory=list)  # 召回表结构（注入生成 prompt）
    recalled_calibers: list[dict] = field(default_factory=list)  # 召回口径（logic 优先）
    generated_sql: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    result: dict | None = None
    reject_reason: str | None = None


class Event(dict):
    """SSE 事件：node / clarify / result / reject / escalate / error。"""


class Pipeline:
    """编排器：按 HLD §4.2 转移图驱动节点。各节点实现为可替换的 service。"""

    def __init__(self, intent_service, recall_service, generator_service, validator_service,
                 executor_service, answer_service):
        self.intent = intent_service
        self.recall = recall_service
        self.generator = generator_service
        self.validator = validator_service
        self.executor = executor_service
        self.answer = answer_service

    async def run(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        started = time.monotonic()
        try:
            node = Node.INTENT
            # 1. 意图识别
            intent = await self.intent.classify(ctx)
            yield Event(type="node", node=node.value, detail=intent)

            if intent == "forbidden":
                yield Event(type="reject", reason_code="E003", message=E003.default_message)
                return

            if intent == "meta_qa":
                async for ev in self._run_meta_qa(ctx):
                    yield ev
                return

            # 2. 取数主链路：先 RECALL 再判充分性
            #    先召回可避免"查不存在的表/指标"被误判为需要澄清（应 E002 拒答）
            yield Event(type="node", node=Node.RECALL.value)
            recall = await self.recall.recall(ctx.question, ctx.session_id)
            if not recall.tables:
                # 用户点名某张表但不在白名单 → E003（不泄露表是否存在，防枚举）
                raise DataProError(E003 if "表" in ctx.question else E002)
            ctx.recalled_tables = [t["table_name"] for t in recall.tables]
            ctx.recalled_schemas = recall.tables
            ctx.recalled_calibers = recall.calibers

            # 三要素不足 → 澄清（最多 2 轮）
            while ctx.clarify_count <= MAX_CLARIFY_ROUNDS:
                clarified = await self.intent.check_sufficiency(ctx)
                if clarified:
                    break
                ctx.clarify_count += 1
                if ctx.clarify_count > MAX_CLARIFY_ROUNDS:
                    raise DataProError(E001)
                q = await self.intent.ask_clarify(ctx)
                yield Event(type="clarify", question=q, rounds=ctx.clarify_count)
                return  # 等用户下一轮输入，由网关侧重新进入 run()

            async for ev in self._run_query(ctx):
                yield ev
        except DataProError as e:
            yield Event(type="reject" if e.err.code in ("E001", "E002", "E003") else "error",
                        reason_code=e.err.code, message=e.message)
        except Exception:
            yield Event(type="error", reason_code="E010",
                        message="系统内部异常，已转人工处理", trace_id=current_trace_id())
        finally:
            yield Event(type="node", node="DONE", latency_ms=round((time.monotonic() - started) * 1000))

    async def _run_query(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        """生成 ⇄ 校验 → 执行 → 呈现（召回已在 run() 完成）。"""

        # GENERATE ⇄ VALIDATE（最多修正 2 次）
        while True:
            yield Event(type="node", node=Node.GENERATE.value, retry=ctx.generate_retries)
            gen = await self.generator.generate(ctx)
            if gen is None:
                ctx.generate_retries += 1
                if ctx.generate_retries > MAX_GENERATE_RETRIES:
                    yield Event(type="escalate", ticket_id=None)
                    return
                continue
            ctx.generated_sql = gen.sql

            yield Event(type="node", node=Node.VALIDATE.value)
            v = await self.validator.validate(gen.sql, ctx.recalled_tables)
            if v.passed:
                break
            ctx.generate_retries += 1
            if ctx.generate_retries > MAX_GENERATE_RETRIES:
                raise DataProError(E004, message="；".join(v.errors))
            ctx.validation_errors = v.errors  # 带错误回炉修正

        # EXECUTE → PRESENT
        yield Event(type="node", node=Node.EXECUTE.value)
        ctx.result = await self.executor.execute(ctx.generated_sql, ctx.user_id, ctx.session_id)
        yield Event(type="node", node=Node.PRESENT.value)
        summary = await self.answer.summarize(ctx)
        yield Event(type="result", sql=ctx.generated_sql, columns=ctx.result["columns"],
                    rows=ctx.result["rows"], row_count=ctx.result["row_count"],
                    truncated=ctx.result["truncated"], explanation=gen.explanation,
                    assumptions=gen.assumptions, summary=summary)

    async def _run_meta_qa(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        yield Event(type="node", node=Node.RECALL.value)
        recall = await self.recall.recall(ctx.question, ctx.session_id)
        if not recall.tables and not recall.calibers:
            raise DataProError(E002)
        ans = await self.answer.answer_meta(ctx, recall)
        yield Event(type="result", columns=[], rows=[], row_count=0, summary=ans,
                    sql=None, assumptions=[])
