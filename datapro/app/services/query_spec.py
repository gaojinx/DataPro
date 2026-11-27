"""语义中间表示（QuerySpec）：question → IR → SQL。

IR 是确定性代码与模型之间的契约：
- 时间窗由 `time_window` 解析（IR 只保留用户原话 time_phrase，不做日期算术）；
- 指标 / 维度 / 过滤 / 粒度由一次 JSON 抽取得到，可回显、可澄清、可跨轮继承；
- 抽取失败（无模型/超时）时退化为规则抽取，链路不阻塞。
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from app.llm.client import llm_client

logger = logging.getLogger("datapro.spec")

GRAINS = ("total", "day", "week", "month")

SPEC_SYSTEM = (
    "你是 DataPro 的查询规格抽取器，只做抽取，不推理、不补全、不写 SQL。"
    "用户没说的槽位一律留空，禁止猜测。"
    "多轮合并问题（含“本次补充”）中，前序问题里的指标必须原样延续，"
    "本轮只补充时间或筛选条件，不得改换指标。"
    "grain 只表示**输出粒度**：整段时间只要一个数就是 total；"
    "只有用户明确要求按天/按周/按月看序列（如“每天”“趋势”“按周”）才填 day/week/month；"
    "时间跨度本身不决定 grain——问“2026年10月的支付订单数”仍是 total。"
    "输出必须是合法 JSON，且只包含以下字段："
    "metric（指标原词）、time_phrase（时间原话）、grain（total/day/week/month 或维度词）、"
    "dimensions（分组维度原词数组）、filters（对象）、ambiguous（拿不准的槽位名数组）。"
)

_SPEC_HINT = """示例：
问题「上周每个品类的支付订单数」
{"metric": "支付订单数", "time_phrase": "上周", "grain": "品类", "dimensions": ["品类"], "filters": {}, "ambiguous": []}

问题「2026年10月的支付订单数」
{"metric": "支付订单数", "time_phrase": "2026年10月", "grain": "total", "dimensions": [], "filters": {}, "ambiguous": []}

问题「最近7天每天支付订单数」
{"metric": "支付订单数", "time_phrase": "最近7天", "grain": "day", "dimensions": [], "filters": {}, "ambiguous": []}

问题「查一下销售额」
{"metric": "销售额", "time_phrase": "", "grain": "total", "dimensions": [], "filters": {}, "ambiguous": ["time"]}
"""


@dataclass
class QuerySpec:
    metric: str = ""
    time_phrase: str = ""
    grain: str = "total"
    dimensions: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)
    source: str = "rule"          # llm | rule | inherit

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "QuerySpec":
        if not data:
            return cls()
        allowed = {k: data[k] for k in
                   ("metric", "time_phrase", "grain", "dimensions", "filters", "ambiguous", "source")
                   if k in data}
        return cls(**allowed)

    # ---- 继承：本轮缺失的槽位沿用上一轮 ----
    def inherit(self, prev: "QuerySpec | None") -> "QuerySpec":
        if not prev:
            return self
        self.metric = self.metric or prev.metric
        self.time_phrase = self.time_phrase or prev.time_phrase
        if not self.dimensions:
            self.dimensions = list(prev.dimensions)
        self.filters = {**prev.filters, **self.filters}
        if self.grain == "total" and not self.dimensions and prev.grain != "total":
            self.grain = prev.grain
        return self

    # ---- 回显给用户：我理解成了什么 ----
    def display(self) -> str:
        parts = []
        if self.time_phrase:
            parts.append(self.time_phrase)
        if self.metric:
            parts.append(self.metric)
        if self.dimensions:
            parts.append("按" + "、".join(self.dimensions))
        elif self.grain in ("day", "week", "month"):
            parts.append({"day": "按天", "week": "按周", "month": "按月"}[self.grain])
        if self.filters:
            parts.append("，".join(f"{k}={v}" for k, v in self.filters.items()))
        return " · ".join(parts) or "未识别出明确规格"

    def rewrite(self) -> str:
        """把 IR 还原成一句完整问题，作为生成输入（替代字符串拼接上下文）。"""
        text = (self.time_phrase or "")
        text += (("的" if text else "") + self.metric) if self.metric else ""
        if self.dimensions:
            text += "，按" + "、".join(self.dimensions) + "分组"
        elif self.grain in ("day", "week", "month"):
            text += "，按" + {"day": "天", "week": "周", "month": "月"}[self.grain] + "汇总"
        if self.filters:
            text += "，条件：" + "，".join(f"{k}={v}" for k, v in self.filters.items())
        return text or ""


_DIM_PATTERN = re.compile(r"按\s*([\u4e00-\u9fa5]{2,4}?)(?=拆分|拆|分组|汇总|统计|看|一下|的|$)")
_DIM_STOP = ("天", "周", "月", "日")
# 槽位替换：换成/改成/改为/换到 + 指标或维度词
_REPLACE_SLOT = re.compile(r"(?:换成|改成|改为|换到|改到|换个)(?:按)?([\u4e00-\u9fa5]{2,6})")
_KNOWN_DIMS = ("渠道", "品类", "城市", "地区", "平台", "性别", "年龄段", "来源", "新老客")
_METRIC_FALLBACK = ("订单数", "订单量", "销售额", "销量", "GMV", "gmv", "用户数", "日活", "活跃用户",
                    "转化率", "退货率", "客单价")


def normalize_grain(spec: "QuerySpec", window) -> None:
    """按时间窗校正输出粒度：窗口只覆盖一个时间桶时，"按天/月分组"是冗余的。

    例如「2026年10月的支付订单数」模型常给 grain=month，但用户要的是一个数，
    应回落为 total，否则会返回"每月一行"的序列。
    """
    if spec is None or window is None or spec.dimensions:
        return
    if spec.grain == "day" and window.days() == 1:
        spec.grain = "total"
    elif spec.grain == "month":
        if window.start.day == 1 and (window.end + timedelta(days=1)).day == 1:
            spec.grain = "total"
    elif spec.grain == "week" and window.days() == 7 and window.start.weekday() == 0:
        spec.grain = "total"


class SpecService:
    """IR 抽取：LLM 优先，失败退化为规则；两者都识别不出也照常往下走。"""

    async def extract(self, question: str, prev: dict | None = None,
                      calibers: list[dict] | None = None) -> QuerySpec:
        base = QuerySpec.from_dict(prev)
        data = None
        if llm_client.available():
            data = await self._llm(question, base)
        if data is None:
            return self._rule(question, base, calibers).inherit(base)
        spec = QuerySpec(
            metric=str(data.get("metric") or "").strip(),
            time_phrase=str(data.get("time_phrase") or "").strip(),
            grain=str(data.get("grain") or "total").strip() or "total",
            dimensions=[str(d).strip() for d in (data.get("dimensions") or []) if str(d).strip()],
            filters={str(k): str(v) for k, v in (data.get("filters") or {}).items()},
            ambiguous=[str(a) for a in (data.get("ambiguous") or [])],
            source="llm",
        )
        # 多轮追问的指标必须继承：合并问题里“本次补充”之前 / “沿用上文”之后的口径词是强约束，
        # 模型偶尔会把「支付订单数」改写成「销售额」，这里用规则命中兜回来。
        prior = ""
        if "本次补充" in question:
            prior = question.split("；本次补充")[0]
        elif "沿用上文：" in question:
            prior = question.split("沿用上文：", 1)[1].rstrip("）")
        if prior:
            prior_spec = self._rule(prior, QuerySpec(), calibers)
            if prior_spec.metric:
                spec.metric = prior_spec.metric
        return spec.inherit(base)

    async def _llm(self, question: str, base: QuerySpec) -> dict | None:
        prev_text = base.rewrite() or "无"
        prompt = (f"{_SPEC_HINT}\n上一轮规格（本轮缺失项可延续）：{prev_text}\n"
                  f"用户本轮问题：{question}\n\n只输出 JSON。")
        try:
            return await llm_client.structured_call(prompt, system=SPEC_SYSTEM,
                                                    tool_version="spec_v1")
        except Exception as exc:  # noqa: BLE001 —— 抽取失败退规则，不阻塞主链路
            logger.warning("规格抽取失败，退规则：%s", exc)
            return None

    def _rule(self, question: str, base: QuerySpec, calibers: list[dict] | None) -> QuerySpec:
        q = question or ""
        spec = QuerySpec(source="rule")
        # 指标：优先命中召回口径的名称/别名，其次通用词
        for c in calibers or []:
            names = [c.get("metric_name", "")] + list(c.get("aliases") or [])
            hit = next((n for n in names if n and n in q), "")
            if hit:
                spec.metric = hit
                break
        if not spec.metric:
            spec.metric = next((w for w in _METRIC_FALLBACK if w in q), "")
        # 槽位替换：「换成销售额」「维度换成渠道」——识别后覆盖上一轮，而不是叠加
        replaced = [w for w in _METRIC_FALLBACK + _KNOWN_DIMS
                    if _REPLACE_SLOT.search(q) and re.search(
                        rf"(?:换成|改成|改为|换到|改到|换个)(?:按)?{re.escape(w)}", q)]
        for word in replaced:
            if word in _METRIC_FALLBACK:
                spec.metric = word
                break
        # 维度：按 X 分组
        dims = [d for d in _DIM_PATTERN.findall(q) if not d.startswith(_DIM_STOP)]
        for word in replaced:
            if word in _KNOWN_DIMS:
                dims = [word]
                break
        spec.dimensions = dims
        if dims:
            spec.grain = dims[0]
        elif any(k in q for k in ("每天", "每日", "按天", "按日")):
            spec.grain = "day"
        elif any(k in q for k in ("每周", "按周")):
            spec.grain = "week"
        elif any(k in q for k in ("每月", "按月")):
            spec.grain = "month"
        else:
            spec.grain = "total"
        # 无法识别的关键槽位交给澄清
        if not spec.metric:
            spec.ambiguous.append("metric")
        return spec


spec_service = SpecService()
