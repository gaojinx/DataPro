"""数据边界问答：回答「数据最新到几号 / 数据覆盖哪些日期」这类元数据问题。

这类问题不含指标口径，召回必然 0 命中，原先会被当成「范围外」拒答。
这里改为对白名单内带 dt 分区的表执行 MIN/MAX 探针，答案直接来自数仓分区，
既回答了问题，也不会编造数值。
"""
import logging
import re

from app.services.executor import executor_service
from app.services.metadata_store import metadata_store

logger = logging.getLogger("datapro.boundary")

# 只认明确的「数据边界 / 新鲜度」问法，避免把普通取数问题（如「最近 7 天销售额」）误判
_BOUNDARY_PATTERNS = (
    r"(数据|数仓|数据库|分区).{0,6}(到|更新到|更新至|截止|截至).{0,6}"
    r"(几号|哪天|哪一天|什么时候|什么日期|何时)",
    r"(数据|数仓|数据库|分区).{0,6}(最新|最晚|最后).{0,6}(是|到|为)?.{0,4}"
    r"(几号|哪天|哪一天|什么时候|什么日期|何时)",
    r"(最新|最晚|最后).{0,4}(数据|分区|日期).{0,6}(到|是|为|有).{0,4}"
    r"(几号|哪天|哪一天|什么时候|什么日期|何时)",
    r"(数据|数仓|数据库).{0,6}(覆盖|包含).{0,6}(哪|几)",
    r"(数据|数仓|数据库).{0,4}(时间|日期).{0,2}(范围|区间)",
    r"(有|共有|一共).{0,2}(多少天|几天).{0,4}数据",
    r"数据.{0,4}(有|共|一共).{0,4}(多少天|几天)",
)


def detect(question: str) -> bool:
    """是否在问数据边界（最新分区 / 覆盖日期范围 / 有多少天数据）。"""
    q = (question or "").strip()
    if not q:
        return False
    return any(re.search(p, q) for p in _BOUNDARY_PATTERNS)


def _date_tables(user_id: str, user_role: str, question: str = "") -> list[dict]:
    """带 dt 分区的表；普通用户按 A02 数据表权限过滤。

    问题点名了某张表（如「订单表最新数据到几号」）时只探该表；
    点名了但命中不到（如「工单表」）返回空，交给常规链路按 E003 处理。
    """
    tables = [t for t in metadata_store.tables()
              if any(str(c).lower() == "dt" for c in (t.get("columns") or []))]
    if user_role != "admin":
        from app.services.db_access import db_access
        allowed = db_access.tables_for(user_id)
        if allowed:
            tables = [t for t in tables if t["table_name"] in allowed]
    q = (question or "").lower()
    if not q:
        return tables
    from app.services.recall import _table_match
    hits = [t for t in tables
            if _table_match(t, q) or any(str(k).lower() in q for k in t.get("keywords") or [])]
    if hits:
        return hits
    return [] if "表" in q else tables


def _fmt(value) -> str:
    text = "" if value is None else str(value)
    return text[:10] if text else "—"


async def probe(user_id: str = "", session_id: str = "", user_role: str = "user",
                question: str = "") -> dict | None:
    """读取各表 dt 的最小/最大分区与天数；失败返回 None（由调用方降级）。"""
    tables = _date_tables(user_id, user_role, question)
    if not tables:
        return None
    parts = []
    for t in tables:
        full = f"{t['schema']}.{t['table_name']}" if t.get("schema") else t["table_name"]
        parts.append(f"SELECT '{t['table_name']}' AS table_name, MIN(dt) AS min_dt, "
                     f"MAX(dt) AS max_dt, COUNT(DISTINCT dt) AS day_cnt FROM {full}")
    sql = " UNION ALL ".join(parts)
    try:
        res = await executor_service.execute(sql, user_id, session_id)
    except Exception as exc:  # noqa: BLE001 —— 探针失败按普通链路降级
        logger.warning("数据边界探针失败：%s", exc)
        return None

    rows = []
    for r in res.get("rows") or []:
        if r.get("max_dt") is None:
            continue
        rows.append({
            "表": r.get("table_name"),
            "最早分区": _fmt(r.get("min_dt")),
            "最新分区": _fmt(r.get("max_dt")),
            "天数": r.get("day_cnt"),
        })
    if not rows:
        return None
    rows.sort(key=lambda x: x["最新分区"], reverse=True)
    return {"columns": ["表", "最早分区", "最新分区", "天数"], "rows": rows,
            "row_count": len(rows)}


def build_summary(rows: list[dict]) -> str:
    """答案优先：先给最新日期，再说明覆盖范围与未同步的表。"""
    latest = rows[0]["最新分区"]
    earliest = min(r["最早分区"] for r in rows)
    if len(rows) == 1:
        row = rows[0]
        return (f"{row['表']} 最新数据到 {latest}，覆盖 {earliest} 至 {latest}"
                f"（共 {row['天数']} 天）。")
    lines = [f"数据库最新数据到 {latest}，现有数据覆盖 {earliest} 至 {latest}。"]
    stale = [r for r in rows if r["最新分区"] != latest]
    if stale:
        detail = "、".join(f"{r['表']}（{r['最新分区']}）" for r in stale[:5])
        lines.append(f"其余表最新分区略早：{detail}。")
    else:
        lines.append("订单、用户、品类/渠道/商品日汇总、投放与退款明细的最新分区一致。")
    return "\n".join(lines)
