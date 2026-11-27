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
import logging
import time
from collections.abc import AsyncGenerator
from decimal import Decimal
from dataclasses import dataclass, field

from app.config import settings
from app.core.errors import DataProError, E001, E002, E003
from app.core.time_window import (TimeWindow, anchor_date, default_window,
                                  resolve_question)
from app.core.trace import current_trace_id
from app.services.caliber_ambiguity import ambiguous_groups, clarify_text, option_view
from app.services.caliber_rulings import caliber_rulings
from app.services import data_boundary, masking

logger = logging.getLogger("datapro.pipeline")


class Node(str, enum.Enum):
    INTENT = "INTENT"
    CLARIFY = "CLARIFY"
    PARSE = "PARSE"
    RECALL = "RECALL"
    GENERATE = "GENERATE"
    VALIDATE = "VALIDATE"
    EXECUTE = "EXECUTE"
    PRESENT = "PRESENT"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


MAX_CLARIFY_ROUNDS = 2
MAX_GENERATE_RETRIES = 2

# 多轮追问补全：当前句缺少指标要素时，与上一轮用户问题合并理解
METRIC_HINTS = ("订单", "用户", "销售额", "销量", "活跃", "数量", "数", "率", "金额", "GMV", "gmv")
# 槽位替换词：本轮改成新时间/新指标/新维度，前序问题只用来补缺失槽位
REPLACE_HINTS = ("换成", "改成", "改为", "换到", "改到", "换个")

# 越界关键词（骨架用规则实现意图分类，接 LLM 后替换为 prompt 意图识别）
FORBIDDEN_PATTERNS = ("update", "delete", "drop", "insert", "truncate", "改", "删", "写入", "导入", "修改",
                      "导出", "下载", "dump", "export", "手机号", "身份证", "银行卡", "密码")
META_QA_PATTERNS = ("口径", "什么意思", "怎么算", "怎么计算", "怎么定义", "什么是", "定义", "字段含义", "有哪些值",
                    "干嘛用", "干什么用", "做什么用", "是做什么", "用途", "表结构", "字段有哪些",
                    "有哪些字段", "有什么字段", "包含哪些字段")


@dataclass
class PipelineContext:
    """一次取数请求的全量上下文。"""
    question: str
    user_id: str
    user_role: str = "user"                                 # admin 不受用户级表白名单限制（A02）
    session_id: str = ""
    trace_id: str = ""
    history: list[dict] = field(default_factory=list)
    raw_question: str = ""                                  # 本轮用户原话（不拼接上下文）
    question_context: list[str] = field(default_factory=list)  # 本轮回答依赖的前序用户问题（按时间顺序）
    clarify_count: int = 0
    generate_retries: int = 0
    recalled_tables: list[str] = field(default_factory=list)  # 白名单凭据：只有这里出现过的表允许出现在 SQL 中
    recalled_schemas: list[dict] = field(default_factory=list)  # 召回表结构（注入生成 prompt）
    recalled_calibers: list[dict] = field(default_factory=list)  # 召回口径（logic 优先）
    recalled_knowledge: list[dict] = field(default_factory=list)  # 命中的行业知识库词条
    memories: list[dict] = field(default_factory=list)            # A11：用户长期记忆（生成参考）
    image_notes: str = ""                   # 视觉模型对上传图片的描述（仅作上下文，不作口径依据）
    time_window: TimeWindow | None = None   # 已确定的时间窗（日期算术不交给模型）
    time_unresolved: bool = False           # 时间表达有歧义 → 必须澄清
    spec: object | None = None              # 语义中间表示 QuerySpec（本轮）
    prev_spec: dict | None = None           # 上一轮槽位（会话级，用于继承）
    self_check_notes: list[str] = field(default_factory=list)  # 结果自检说明（不改数值）
    ruling_notes: list[str] = field(default_factory=list)      # D03 口径裁决说明（进「依据」）
    retry_stage: int = 0                    # 结构化重试阶段（0 正常 / 1 带错回炉 / 2 最小查询）
    no_sql_count: int = 0                   # 模型未给出 SQL 的次数（决定是否强制出 SQL）
    generated_sql: str | None = None
    sql_final: str | None = None            # 校验器改写后的 SQL（含强制 LIMIT），执行与留痕都以它为准
    validation_errors: list[str] = field(default_factory=list)
    outcome: str = ""                       # result/reject/escalate/clarify/error（落库统计用）
    error_code: str = ""                    # 对应 errors.E00x
    forced_caliber_id: str | None = None    # 用户在口径澄清中选定的口径（下一轮注入）
    caliber_clarified: bool = False         # 本会话已问过口径歧义（避免反复追问）
    caliber_options: list[dict] = field(default_factory=list)  # 本次口径候选（透传给网关保存）
    result: dict | None = None
    reject_reason: str | None = None
    langfuse_trace_id: str = ""             # 可观测：Langfuse trace（反馈打分时回填）


class Event(dict):
    """SSE 事件：node / clarify / result / reject / escalate / error。"""


class Pipeline:
    """编排器：按 HLD §4.2 转移图驱动节点。各节点实现为可替换的 service。"""

    def __init__(self, intent_service, recall_service, generator_service, validator_service,
                 executor_service, answer_service, spec_service=None):
        self.intent = intent_service
        self.recall = recall_service
        self.generator = generator_service
        self.validator = validator_service
        self.executor = executor_service
        self.answer = answer_service
        self._spec = spec_service

    def _spec_service(self):
        if self._spec is None:      # 懒加载：避免 core 与服务层的模块级循环依赖
            from app.services.query_spec import spec_service
            self._spec = spec_service
        return self._spec

    async def run(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        started = time.monotonic()
        try:
            raw_question = ctx.question
            ctx.raw_question = raw_question
            ctx.question = self._with_context(ctx)
            node = Node.INTENT
            # 1. 意图识别
            intent = await self.intent.classify(ctx)
            yield Event(type="node", node=node.value, detail=intent)

            if intent == "forbidden":
                yield Event(type="reject", reason_code="E003", message=E003.default_message)
                return

            # 数据边界（「数据最新到几号」「数据覆盖哪些日期」）：这类问题没有指标口径，
            # 召回必然 0 命中，直接读真实分区作答，避免被当成范围外拒答
            if data_boundary.detect(ctx.raw_question or ctx.question):
                yield Event(type="node", node=Node.RECALL.value)
                boundary = await data_boundary.probe(ctx.user_id, ctx.session_id, ctx.user_role,
                                                     ctx.raw_question or ctx.question)
                if boundary:
                    yield Event(type="node", node=Node.EXECUTE.value)
                    ctx.result = boundary
                    # 边界问答本身就是新鲜度答案：取各表最新分区的最大值作为截止日
                    latest_dt = max((r.get("最新分区") for r in boundary["rows"]), default=None)
                    yield Event(type="result", columns=boundary["columns"],
                                rows=boundary["rows"], row_count=boundary["row_count"],
                                truncated=False, sql=None, assumptions=[],
                                explanation="直接读取数仓各表 dt 分区范围（MIN/MAX），未经过模型生成",
                                data_as_of=latest_dt, data_as_of_basis="dt 分区探针",
                                summary=data_boundary.build_summary(boundary["rows"]))
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
                if "表" in ctx.question:
                    raise DataProError(E003)
                # 有可用模型时先由模型说明可查询范围与替代问法（不编造数值）
                scope = await self.answer.explain_scope(ctx, recall)
                if scope:
                    yield Event(type="result", columns=[], rows=[], row_count=0, summary=scope,
                                sql=None, assumptions=[], explanation="当前数仓未覆盖该指标，已说明可查询范围",
                                sources=self._sources([], recall.calibers, []))
                    return
                raise DataProError(E002)
            ctx.recalled_tables = [t["table_name"] for t in recall.tables]
            ctx.recalled_schemas = recall.tables
            ctx.recalled_calibers = recall.calibers
            # 用户已明确口径：只保留该口径，后续 IR/生成/prompt 都以它为准
            if ctx.forced_caliber_id:
                chosen = [c for c in ctx.recalled_calibers
                          if c.get("metric_id") == ctx.forced_caliber_id]
                if chosen:
                    ctx.recalled_calibers = chosen
            ctx.recalled_knowledge = recall.knowledge

            # 语义中间表示：原始问题 + 上一轮槽位 → 本轮 QuerySpec（缺失槽位继承上一轮）
            yield Event(type="node", node=Node.PARSE.value)
            ctx.spec = await self._spec_service().extract(
                raw_question, ctx.prev_spec, ctx.recalled_calibers)
            yield Event(type="node", node="SPEC", detail=ctx.spec.display())

            # 时间窗：IR 的时间原话优先，否则回到原始问题；仍无则沿用上一轮
            self._bind_time(ctx, raw_question, ctx.spec.time_phrase)
            self._normalize_grain(ctx)
            yield Event(type="node", node="TIME",
                        detail=("需确认时间" if ctx.time_unresolved
                                else (ctx.time_window.text() if ctx.time_window else "未指定")))

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

            # 口径歧义（P1）：同一指标词命中多个口径 → 先确认口径，不猜
            if not ctx.forced_caliber_id:
                groups = ambiguous_groups(ctx.recalled_calibers, ctx.question)
                if groups:
                    # D03：管理员已裁决过的歧义词直接采用裁决口径，并在依据里声明
                    unresolved = []
                    for g in groups:
                        ruling = caliber_rulings.resolve(g["term"])
                        option_ids = {o.get("metric_id") for o in g["options"]}
                        if ruling and ruling.get("metric_id") in option_ids:
                            picked = ruling["metric_id"]
                            ctx.recalled_calibers = [c for c in ctx.recalled_calibers
                                                     if c.get("metric_id") == picked]
                            ctx.ruling_notes.append(
                                f"口径「{g['term']}」按管理员裁决取 {picked}"
                                f"{('（' + ruling['note'] + '）') if ruling.get('note') else ''}")
                        else:
                            unresolved.append(g)
                    groups = unresolved
                if groups:
                    if ctx.caliber_clarified:
                        raise DataProError(
                            E001, message="该指标存在多种统计口径，请明确口径后重新提问")
                    group = groups[0]
                    ctx.caliber_options = [option_view(o) for o in group["options"]]
                    yield Event(type="clarify", kind="caliber", question=clarify_text(group),
                                options=ctx.caliber_options,
                                original_question=ctx.raw_question)
                    return

            async for ev in self._run_query(ctx):
                yield ev
        except DataProError as e:
            yield Event(type="reject" if e.err.code in ("E001", "E002", "E003") else "error",
                        reason_code=e.err.code, message=e.message)
        except Exception:
            logger.exception("流水线异常 trace_id=%s", current_trace_id())
            yield Event(type="error", reason_code="E010",
                        message="系统内部异常，已转人工处理", trace_id=current_trace_id())
        finally:
            yield Event(type="node", node="DONE", latency_ms=round((time.monotonic() - started) * 1000))

    @staticmethod
    def _with_context(ctx: PipelineContext) -> str:
        """多轮追问补全：如上一轮"查昨天的支付订单数"，本轮"那上周呢"，合并为完整问题。

        仅在当前句不含指标要素时合并，避免影响独立问题。
        """
        q = (ctx.question or "").strip()
        users = [m.get("content", "") for m in ctx.history if m.get("role") == "user"]
        # 兼容两种调用方式：history 含本轮（线上）或不含本轮（部分单测/外部调用）
        current_included = bool(users) and users[-1].strip() == q
        priors = users[:-1] if current_included else users
        if not priors:
            return q
        # 往回找最近一条带指标要素的问题，跳过"那上周呢""按品类拆分"这类追问
        prev = ""
        for u in reversed(priors[-3:]):
            if any(w in u for w in METRIC_HINTS):
                prev = u.strip()
                break
        if not prev or any(w in q for w in METRIC_HINTS):
            return q
        # 记录本轮回答依赖的前序提问（含被跳过的中间轮次），供审计与 golden 复现
        ctx.question_context = [u.strip() for u in priors[-3:] if u.strip()]
        # 槽位替换：本轮指定了时间/维度/指标时本轮在前，时间解析取第一个命中
        replacement = any(h in q for h in REPLACE_HINTS)
        if not replacement:
            from app.core.time_window import resolve_question as _resolve
            replacement = _resolve(q)[0] is not None
        if replacement:
            return f"{q}；本次补充（时间/条件优先于上句）：{prev}"
        return f"{prev}；本次补充（时间/条件优先于上句）：{q}"

    @staticmethod
    def _bind_time(ctx: PipelineContext, raw_question: str, phrase: str = "") -> None:
        """确定时间窗：IR 时间原话 → 本轮原句 → 上一轮继承 → 默认最近 7 天。

        日期算术一律由 time_window 负责，模型只负责翻译，避免「问上月、查近 7 天」。
        """
        # 本轮原句的时间优先级最高（IR 的 time_phrase 可能是上一轮继承来的）
        window, ambiguous = resolve_question(raw_question)
        if window is None and phrase:
            window, ambiguous = resolve_question(phrase)
        if ambiguous:
            ctx.time_unresolved = True
            return
        if window is None and phrase:
            # IR 给的时间原话解析不出（如只说了"最近"）→ 回到原句再试一次
            window, ambiguous = resolve_question(raw_question)
            if ambiguous:
                ctx.time_unresolved = True
                return
        if window is None:
            # 本轮原句没有时间要素时，再解析合并句（"本次补充"后可能出现新时间）
            if ctx.question and ctx.question != raw_question:
                window, ambiguous = resolve_question(ctx.question)
                if ambiguous:
                    ctx.time_unresolved = True
                    return
        if window is None:
            # 多轮追问（"按品类拆分一下"）沿用上一轮的时间窗
            users = [m.get("content", "") for m in ctx.history if m.get("role") == "user"]
            current_included = bool(users) and users[-1].strip() in (
                (ctx.question or "").strip(), (ctx.raw_question or "").strip())
            prev_users = users[:-1] if current_included else users
            for u in reversed(prev_users[-3:]):
                inherited, amb = resolve_question(u)
                if amb:
                    break
                if inherited:
                    window = inherited
                    break
        ctx.time_window = window or default_window()

    @staticmethod
    def _spec_issues(ctx: PipelineContext, sql: str) -> list[str]:
        """规格与 SQL 形状的一致性检查（规则版，不调模型）。"""
        from app.services.spec_check import check
        return check(getattr(ctx, "spec", None), sql, ctx.question)

    @staticmethod
    def _normalize_grain(ctx: PipelineContext) -> None:
        """窗口只覆盖一个时间桶时，"按天/月分组"应回落为整体汇总。"""
        spec = getattr(ctx, "spec", None)
        if spec is None or ctx.time_window is None:
            return
        from app.services.query_spec import normalize_grain
        normalize_grain(spec, ctx.time_window)

    async def _run_query(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        """生成 ⇄ 校验 → 执行 → 呈现（召回已在 run() 完成）。"""

        # GENERATE ⇄ VALIDATE（最多修正 2 次）：每轮都改变输入，不做同义反复
        while True:
            ctx.retry_stage = ctx.generate_retries
            yield Event(type="node", node=Node.GENERATE.value, retry=ctx.generate_retries)
            gen = await self.generator.generate(ctx)
            if gen is None:
                ctx.no_sql_count += 1
                ctx.generate_retries += 1
                if ctx.generate_retries > MAX_GENERATE_RETRIES:
                    # 终态事件必须带 error_code，否则记录里只剩 outcome=escalate、
                    # 错误码为空，按 E001–E010 统计错误率时这部分直接消失
                    yield Event(type="escalate", ticket_id=None, reason="NO_SQL",
                                reason_code="E010",
                                message="模型本次未能生成可执行的查询语句，已转人工处理；"
                                        "可以换个问法，或补充时间范围与指标后再试")
                    return
                continue
            ctx.generated_sql = gen.sql

            yield Event(type="node", node=Node.VALIDATE.value)
            # A02：普通用户若配置了数据表权限，只允许访问授权表；管理员不受限
            user_tables = None
            if ctx.user_role != "admin":
                from app.services.db_access import db_access
                user_tables = db_access.tables_for(ctx.user_id)
            v = await self.validator.validate(gen.sql, ctx.recalled_tables,
                                              self._column_map(ctx), user_tables=user_tables)
            if v.forbidden:
                # 越权表直接拒答（E003），不重试：重试也无法获得权限。
                # 附带表名让前端能一键预填申请，不暴露表结构或其他数据。
                yield Event(type="reject", reason_code="E003",
                            message=f"该查询涉及您无权访问的数据表：{', '.join(v.forbidden)}；"
                                    f"可在「设置 → 权限申请」提交开通申请，或联系管理员",
                            tables=list(v.forbidden), can_request=True)
                return
            if v.passed:
                ctx.sql_final = v.sql_final or gen.sql
                # IR 一致性：规格与 SQL 形状对不上（如要单值却 GROUP BY）时回炉一次
                issues = self._spec_issues(ctx, gen.sql)
                if issues:
                    if ctx.generate_retries == 0:
                        ctx.generate_retries += 1
                        ctx.validation_errors = issues
                        continue
                    # 回炉一次仍不一致：宁可转人工，也不返回「看起来正确」的错误结果
                    yield Event(type="escalate", ticket_id=None, reason="IR_INCONSISTENT",
                                reason_code="E010", message="；".join(issues))
                    return
                break
            ctx.generate_retries += 1
            if ctx.generate_retries > MAX_GENERATE_RETRIES:
                raise DataProError(E004, message="；".join(v.errors))
            ctx.validation_errors = v.errors  # 带错误回炉修正

        # EXECUTE → 自检 → PRESENT
        yield Event(type="node", node=Node.EXECUTE.value)
        estimated = await self.executor.estimate_scan(ctx.generated_sql, ctx.session_id, ctx.time_window)
        if estimated and estimated > settings.max_scan_rows:
            # A09：预估扫描量超阈值，宁可转人工也不让大查询拖垮数仓
            yield Event(type="escalate", ticket_id=None, reason="SCAN_TOO_LARGE",
                        reason_code="E009",
                        message=(f"预计扫描约 {estimated:,} 行，超过上限 {settings.max_scan_rows:,} 行；"
                                 f"请缩小时间范围或增加筛选条件后重试"))
            return
        ctx.result = await self.executor.execute(ctx.generated_sql, ctx.user_id,
                                                 ctx.session_id, ctx.time_window)
        ctx.self_check_notes = await self._self_check(ctx)
        yield Event(type="node", node=Node.PRESENT.value)
        summary = await self.answer.summarize(ctx)
        # 返回层脱敏（S04）：表格/CSV/审计拿到的都是打码后的行，数值列不受影响
        rows, mask_stats = masking.mask_rows(ctx.result["rows"], ctx.result["columns"])
        # U-B01/U-F05：只在真正探测到分区时才下发截止日，不拿日历 T-1 冒充
        from app.services.partition_clock import partition_clock

        confirmed_as_of = partition_clock.confirmed()
        memory_notes = [str(m.get("content") or "").strip()[:80]
                        for m in (ctx.memories or []) if str(m.get("content") or "").strip()][:5]
        # X-R04：在 default=str 之前按 pymysql 原生类型推断数值列白名单。
        # DECIMAL 金额序列化后与订单号同为字符串，前端只能靠这个白名单区分，禁止启发式格式化。
        raw_rows, raw_cols = ctx.result["rows"], ctx.result["columns"]
        numeric_columns = [
            c for c in raw_cols
            if any(isinstance(r.get(c), (int, float, Decimal)) for r in raw_rows)
            and all(r.get(c) is None or isinstance(r.get(c), (int, float, Decimal))
                    for r in raw_rows)
        ]
        yield Event(type="result", sql=ctx.generated_sql, columns=ctx.result["columns"],
                    rows=rows, row_count=ctx.result["row_count"],
                    truncated=ctx.result["truncated"], explanation=gen.explanation,
                    assumptions=list(gen.assumptions) + ctx.self_check_notes + ctx.ruling_notes,
                    summary=summary, time_window=ctx.time_window.text() if ctx.time_window else None,
                    time_window_start=ctx.time_window.start.isoformat() if ctx.time_window else None,
                    time_window_end=ctx.time_window.end.isoformat() if ctx.time_window else None,
                    data_as_of=confirmed_as_of.isoformat() if confirmed_as_of else None,
                    data_as_of_basis=(settings.partition_anchor_table if confirmed_as_of else None),
                    memory_notes=memory_notes or None,
                    est_scan_rows=estimated,          # X-L03：复用 EXECUTE 前已算出的预估值，不另发 EXPLAIN
                    numeric_columns=numeric_columns,  # X-R04：数值列白名单（display-only）
                    spec=ctx.spec.to_dict() if ctx.spec else None,
                    spec_display=ctx.spec.display() if ctx.spec else None,
                    masked=sum(mask_stats.values()),
                    sources=self._sources(ctx.recalled_knowledge, ctx.recalled_calibers, ctx.recalled_schemas))

    @staticmethod
    def _column_map(ctx: PipelineContext) -> dict[str, set[str]]:
        """召回表的可用字段（含上传文件的中文表头），供校验器做存在性检查。"""
        out: dict[str, set[str]] = {}
        for t in ctx.recalled_schemas or []:
            cols = {str(c).lower() for c in t.get("columns") or []}
            cols |= {str(c).lower() for c in (t.get("column_labels") or [])}
            if cols:
                out[str(t.get("table_name", "")).lower()] = cols
        return out

    async def _self_check(self, ctx: PipelineContext) -> list[str]:
        """执行后语义自检：只产出说明，绝不修改结果数值。"""
        r = ctx.result or {}
        notes: list[str] = []
        if r.get("row_count") == 0 and ctx.time_window:
            probe = await self.executor.probe_window(ctx.time_window, ctx.recalled_schemas,
                                                     ctx.session_id)
            w = ctx.time_window
            if probe == 0:
                notes.append(f"时间窗 {w.start} ~ {w.end}（{w.label}）内该表无数据；"
                             f"数仓最新分区为 {anchor_date()}，可改问「最近 7 天」或指定已有日期")
            elif probe:
                notes.append(f"时间窗内该表共 {probe} 行，但加上筛选条件后为 0 行，请确认过滤条件是否正确")
        elif r.get("row_count") == 1 and len(r.get("columns") or []) == 1:
            value = (r.get("rows") or [{}])[0].get(r["columns"][0])
            if value is None:
                notes.append("聚合结果为空，可能时间窗内无满足条件的记录")
            elif isinstance(value, (int, float)) and value < 0:
                notes.append("聚合结果为负值，请确认口径或条件是否符合预期")
            elif value == 0 and ctx.time_window:
                # 0 单 ≠ 无数据：探针确认时间窗内是否根本没有分区数据
                probe = await self.executor.probe_window(ctx.time_window, ctx.recalled_schemas,
                                                         ctx.session_id)
                w = ctx.time_window
                if probe == 0:
                    notes.append(f"时间窗 {w.start} ~ {w.end}（{w.label}）内该表无任何数据，"
                                 f"结果 0 表示「无数据」而非「业务量为 0」；"
                                 f"数仓最新分区为 {anchor_date()}")
        return notes

    async def _run_meta_qa(self, ctx: PipelineContext) -> AsyncGenerator[Event, None]:
        yield Event(type="node", node=Node.RECALL.value)
        recall = await self.recall.recall(ctx.question, ctx.session_id)
        docs = await self._doc_hits(ctx.question)
        # 文档命中也算「有依据」：只查文档（如制度、规范）的问题不该被 E002 拒答
        if not recall.tables and not recall.calibers and not recall.knowledge and not docs:
            # 与取数分支保持一致的出口：只回一句「未找到相关数据表或指标口径」，
            # 用户既不知道系统能查什么，也不知道换个问法是否可行。有模型就先说清范围。
            scope = await self.answer.explain_scope(ctx, recall)
            if scope:
                yield Event(type="result", columns=[], rows=[], row_count=0, summary=scope,
                            sql=None, assumptions=[],
                            explanation="知识库 / 口径 / 文档均未命中，已说明可查询范围",
                            sources=self._sources([], recall.calibers, []))
                return
            raise DataProError(E002)
        ans = await self.answer.answer_meta(ctx, recall, docs)
        yield Event(type="result", columns=[], rows=[], row_count=0, summary=ans,
                    sql=None, assumptions=[], explanation="命中知识库 / 口径 / 文档",
                    sources=self._sources(recall.knowledge, recall.calibers, recall.tables, docs))

    @staticmethod
    async def _doc_hits(question: str) -> list[dict]:
        """文档 RAG 检索：失败不阻断问答，按「无文档」继续走口径链路。"""
        from app.services.doc_rag import doc_rag

        try:
            return await doc_rag.search(question)
        except Exception as exc:  # noqa: BLE001 —— 检索异常不应让整轮问答失败
            logger.warning("文档检索失败，本轮按无文档处理：%s", exc)
            return []

    @staticmethod
    def _sources(knowledge: list[dict], calibers: list[dict], tables: list[dict],
                 docs: list[dict] | None = None) -> list[dict]:
        """可溯源信息：本次回答依据的知识库词条、口径、表与文档片段。"""
        out = [{"type": "knowledge", "name": k["term"], "detail": k.get("definition", "")}
               for k in knowledge or []]
        out += [{"type": "caliber", "name": c["metric_name"],
                 "detail": c.get("logic", "")} for c in calibers or []]
        out += [{"type": "table", "name": t["table_name"]} for t in tables or []]
        # 文档溯源精确到章节：用户要判断「这句话出自哪里、该不该信」，只给文档名不够
        from app.services.doc_rag import section_label

        for d in docs or []:
            label = section_label(d.get("title", ""), d.get("section", ""))
            out.append({"type": "doc",
                        "name": (d.get("title") or "") + (f" § {label}" if label else ""),
                        "detail": (d.get("text") or "")[:120],
                        "score": d.get("score")})
        return out
