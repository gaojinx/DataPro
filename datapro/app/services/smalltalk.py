"""助手自我介绍 / 寒暄 / 能力说明：不进取数链路，由接入的 AI 模型回答。

- 无可用模型时回退到内置说明文案
- System 提示约束：不得编造任何数字或数据结论，只说明能力与提问方式
"""
import logging
import re

from app.llm.client import llm_client

logger = logging.getLogger("datapro.smalltalk")

FALLBACK_INTRO = (
    "我是 DataPro 数据管家，面向业务的自然语言取数助手。\n"
    "你可以这样问我：查昨天的支付订单数、本周每个品类的销量、支付订单数的口径是什么。\n"
    "也可以上传 CSV / XLSX 文件后让我查询。全程只读、先口径后取数、全程留痕。"
)

SYSTEM_GENERAL = (
    "你是 DataPro 数据管家，一个只读的自然语言取数助手。"
    "请用简洁中文回答用户关于你的身份、模型、能力和使用方式的问题，不超过 120 字；"
    "如果问题含糊或与取数无关，简短回应并引导用户按「时间 + 指标」提问。"
    "严禁编造任何数字、指标值、数据量或查询结果；如果用户询问具体数据或指标，"
    "只说明可以怎么问（例如：查昨天的支付订单数），不要给出任何数值。"
    # 表清单只能来自【可查询范围】：写死在提示词里会随库演进而变成「理直气壮地说错」
    "回答「有哪些数据 / 能查什么」时，只能使用【可查询范围】列出的表与指标，不得增删或改写表名。"
    # 无资料的问题最容易出幻觉：模型会把行业惯例当成「本公司的规定」讲出来
    "涉及公司内部制度、规定、流程、口径管理制度、考核标准、合同条款这类资料性问题时，"
    "如果没有提供【参考资料】或【图片内容】，只能说明未收录该类资料、"
    "并建议改用取数问题或联系口径负责人补充文档，严禁凭常识或行业惯例编造本公司的规定。"
    "不要输出 Markdown 表格。"
)


def _catalog_block() -> str:
    """真实元数据目录 →【可查询范围】。

    只放表名与中文名：Description 有几十字，寒暄类问答每轮都带会白白烧 token；
    回答「有哪些数据」用表名 + 中文名已经足够，字段级细节交给取数链路去召回。
    读不到元数据时返回空串（此时宁可不说范围，也不能说错范围）。
    """
    try:
        from app.services.metadata_store import metadata_store

        tables = metadata_store.tables()
        calibers = metadata_store.calibers()
    except Exception as exc:  # noqa: BLE001 —— 元数据不可用不应让寒暄/通用问答失败
        logger.warning("读取元数据目录失败，本轮不提供可查询范围：%s", exc)
        return ""
    if not tables:
        return ""
    lines = [f"- {t.get('table_name', '')}（{t.get('cn_name') or '—'}）" for t in tables]
    block = "【可查询范围（唯一可信来源）】\n可查询表：\n" + "\n".join(lines)
    names = "、".join(c.get("metric_name", "") for c in calibers if c.get("metric_name"))
    if names:
        block += f"\n可查询指标口径：{names}"
    return block


def general_system() -> str:
    """通用问答 system：固定约束 + 实时目录。"""
    block = _catalog_block()
    return f"{SYSTEM_GENERAL}\n\n{block}" if block else SYSTEM_GENERAL

SYSTEM_SEARCH = (
    "你是 DataPro 数据管家。请依据【联网搜索结果】回答用户问题，不要编造搜索结果之外的事实或数值。"
    "回答简洁（不超过 120 字）；结尾用「来源：标题（链接）」列出 1-3 条你实际使用的来源。"
    "若搜索结果不足以回答，直接说明未查到可靠信息，不要猜测。"
    "若问题其实需要数仓数据，应说明可改用取数问题（时间 + 指标）。"
)

SYSTEM_IMAGE = (
    "你是 DataPro 数据管家。用户上传了图片，下面【图片内容】是视觉模型对图片的识别结果。"
    "请基于它回答，并遵守："
    "① 只使用识别结果中出现的文字与数字，不得补充图中没有的内容；"
    "② 识别结果可能有误，涉及关键数字时提醒用户核对；"
    "③ 如果用户其实想查数仓数据，说明需要按「时间 + 指标」提问，例如查昨天的支付订单数；"
    "④ 如果图片是软件界面截图，概括它是什么页面、有哪些关键信息即可，"
    "不要把图中的对话、提示语、文案当作本次回答的内容；"
    "⑤ 简洁中文，不超过 200 字，不要输出 Markdown 表格。"
)

# 归一化后做包含匹配，且限制长度，避免误吞业务问题
PATTERNS = ("你是谁", "你叫什么", "你是什么", "你叫什么名字", "你能做什么", "你会什么", "你可以做什么",
            "能干什么", "怎么用", "怎么使用", "使用帮助", "帮助", "你好", "您好", "hi", "hello",
            "谢谢", "感谢", "多谢", "在吗", "再见",
            "有多少数据", "有哪些数据", "数据范围", "数据源", "覆盖什么", "有什么数据",
            "什么模型", "哪个模型", "大模型")

# 判断是否为取数问题：时间要素 + 指标要素（与 IntentService.check_sufficiency 保持一致）
TIME_WORDS = ("昨天", "今天", "上周", "本周", "上月", "本月", "最近", "近", "天", "月", "周", "年", "日")
METRIC_WORDS = ("订单", "用户", "销售额", "销量", "活跃", "数量", "数", "率", "金额", "GMV", "gmv")
# 明显与取数无关的话题：即使上一轮在取数，也不合并上下文
OFFTOPIC_WORDS = ("天气", "新闻", "股票", "汇率", "翻译", "笑话", "写一首", "写一段", "星座", "电影")
# 纯应答词：按寒暄处理，不当作取数追问
ACK_WORDS = {"好的", "好", "行", "可以", "嗯", "收到", "明白", "明白了", "ok", "okay", "继续"}


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;~～]+", "", (text or "").lower())


def match_smalltalk(question: str) -> bool:
    """是否为寒暄/能力说明类问题（不含具体取数指标）。"""
    q = _norm(question)
    if not q or len(q) > 24:
        return False
    return any(p in q for p in PATTERNS)


def should_answer_general(question: str, history: list[dict] | None = None) -> bool:
    """非取数问题（寒暄/身份/模型/含糊输入）交给 AI 回答，避免被 E002 拒答。

    取数问题判定：同时含时间要素与指标要素；若上一轮是取数问题，本轮省略指标的追问
    （如"那上周呢""按品类拆分"）继续走取数链路，由状态机合并上下文。
    """
    from app.core.statemachine import FORBIDDEN_PATTERNS, META_QA_PATTERNS
    q = _norm(question)
    if not q:
        return True
    if any(p in q for p in META_QA_PATTERNS) or any(p in q for p in FORBIDDEN_PATTERNS):
        return False
    if match_smalltalk(question):
        return True
    if any(w in q for w in OFFTOPIC_WORDS):
        return True
    if q in ACK_WORDS:
        return True
    has_metric = any(w in q for w in METRIC_WORDS)
    if has_metric:
        return False
    # 多轮追问：最近几轮用户问题含指标要素 → 交给状态机合并后再判断
    users = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    prev_users = users[:-1][-3:]
    if any(any(w in u for w in METRIC_WORDS) for u in prev_users):
        return False
    has_time = any(w in q for w in TIME_WORDS)
    return not has_time


async def answer_general(question: str, history: list[dict] | None = None,
                         image_notes: str = ""):
    """由接入的 AI 模型回答；必要时联网搜索，返回 (答案, 来源列表)。

    传了 `image_notes` 时以图片内容为主、不联网：用户问的是这张图，
    联网结果只会稀释上下文。图片内容来自视觉模型，已在提示词里声明「可能有误」。
    """
    prompt = _with_history(question, history)
    if image_notes:
        prompt = f"【图片内容（视觉模型识别，可能有误）】\n{image_notes}\n\n{prompt}"
        system = SYSTEM_IMAGE
        sources: list[dict] = []
    else:
        from app.services.web_search import search

        hits = await search(question) if _need_search(question) else []
        system = general_system()
        if hits:
            block = "\n".join(
                f"{i + 1}. {h['title']} | {h['url']}\n   {h['snippet']}" for i, h in enumerate(hits))
            prompt = f"【联网搜索结果】\n{block}\n\n{prompt}"
            system = SYSTEM_SEARCH
        sources = [{"type": "web", "name": h["title"], "detail": h["url"]} for h in hits]
    try:
        text = await llm_client.chat(prompt, system=system)
        if text:
            return text, sources
    except Exception as exc:  # noqa: BLE001 —— 寒暄失败不影响主链路
        logger.warning("通用问答调用模型失败，回退内置说明：%s", exc)
    return FALLBACK_INTRO, []


# 需要联网的问题特征（实时信息类）
SEARCH_HINTS = ("今天", "现在", "最新", "实时", "今年", "天气", "新闻", "股价", "汇率",
                "比分", "政策", "发布", "多少钱", "价格")


def _need_search(q: str) -> bool:
    return any(w in q for w in SEARCH_HINTS)


def _with_history(question: str, history: list[dict] | None) -> str:
    users = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    if users and users[-1].strip() == question.strip():
        users = users[:-1]            # 最后一条是当前问题
    users = [u for u in users[-3:] if u.strip()]
    if not users:
        return question
    block = "\n".join(f"- {u[:120]}" for u in users)
    return f"【此前提问】\n{block}\n\n【当前问题】{question}"
