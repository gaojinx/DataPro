"""IR 一致性校验：规格（QuerySpec）与生成的 SQL 是否对得上。

只做确定性检查，不调用模型；且**只在抽取可信时生效**（source=llm），
规则抽取的 spec 本身可能漏维度，不能反过来否定 SQL。

三条规则：
1. 规格是整体汇总（total）且无维度 → SQL 不应有顶层 GROUP BY；
2. 规格有维度 → SQL 应有顶层 GROUP BY；
3. 规格点名了时间 → SQL 必须用 {date_start}/{date_end} 占位符（禁止硬编码日期）。
"""
from __future__ import annotations

import logging

import sqlglot

logger = logging.getLogger("datapro.spec_check")

# 出现这些词说明用户其实想要分组，只是 spec 没抽出来；此时不做 total 判定，避免误伤
_DIM_HINTS = ("各", "每", "按", "分", "top", "TOP", "排行", "分布", "拆分", "对比")
# 明细/列表类诉求（"退款的数据给我看看"）本来就不该是单值，不能按 total 规则回炉
_DETAIL_HINTS = ("看看", "明细", "列表", "记录", "哪几", "哪些", "全部", "列出", "展示")


def _top_group(parsed) -> bool:
    """只看顶层 GROUP BY，避免把子查询里合法的分组当成不一致。"""
    return parsed.args.get("group") is not None


def _has_placeholder(sql: str) -> bool:
    return any(token in sql for token in ("{date_start}", "{date_end}", "{start_date}", "{end_date}", "{date}"))


def check(spec, sql: str, question: str = "") -> list[str]:
    """返回不一致项（中文错误描述）；空列表表示一致或无需校验。"""
    if spec is None or not sql:
        return []
    try:
        parsed = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return []          # 语法问题交给 validator，不在这里重复报错

    errors: list[str] = []
    grouped = _top_group(parsed)
    dims = list(getattr(spec, "dimensions", None) or [])
    source = getattr(spec, "source", "rule")

    if source == "llm":
        # 只校验「用户真的说了」的维度；spec 里凭空多出的维度不算约束，避免误伤
        asked_dims = [d for d in dims if d and d in (question or "")]
        wants_dim = bool(asked_dims) or any(h in (question or "") for h in _DIM_HINTS)
        wants_detail = any(h in (question or "") for h in _DETAIL_HINTS)
        uncertain_metric = "metric" in (getattr(spec, "ambiguous", None) or [])
        if getattr(spec, "grain", "total") == "total" and not dims:
            if grouped and not (wants_dim or wants_detail or uncertain_metric):
                errors.append("规格是整体汇总（只要一个数），但 SQL 使用了 GROUP BY；请去掉分组后重写")
        elif asked_dims and not grouped:
            errors.append(f"规格要求按 {'、'.join(asked_dims)} 分组，但 SQL 没有 GROUP BY；请补上分组")

    if getattr(spec, "time_phrase", ""):
        if not _has_placeholder(sql):
            errors.append("规格指定了时间范围，但 SQL 未使用 {date_start}/{date_end} 占位符（禁止硬编码日期）")
    return errors
