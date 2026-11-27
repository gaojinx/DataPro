"""把历史问答（query_record）转成 golden 评测候选。

设计原则（对应「看起来聪明但实际有害」的陷阱 #5：👍 只是「用户以为对」）：
- 不自动并入评测集：只产出 draft（reviewed=false），人工过一遍改 reviewed=true 后再 --promote；
- 入库前重跑一次校验与执行，SQL 已失效的直接标 suspect，不让坏样本污染回归；
- 👎 不进 golden，单独输出 failure_taxonomy，用于归因复盘；
- 相对时间（昨天/最近7天）的 golden 会随数据日期漂移，标 volatile，提醒重跑。
- 重跑 harvest 会覆盖草稿，但按 (history, question) 保留人工复核结果：`reviewed`、
  手工断言（must_not/min_rows/assumption_contains）与以「复核：」开头的备注；
  时间窗与 golden 数值仍用本轮重跑结果（数据日期可能已滚动）。

用法：
    python -m eval.harvest                 # 生成 eval/datasets/harvest_draft.jsonl
    python -m eval.harvest --days 30 --limit 200
    python -m eval.harvest --promote       # 把 reviewed=true 的草稿并入 eval_golden.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
import sqlglot
from sqlglot import exp

logger = logging.getLogger("datapro.harvest")

DATASET_DIR = Path(__file__).parent / "datasets"
DRAFT_PATH = DATASET_DIR / "harvest_draft.jsonl"
GOLDEN_PATH = DATASET_DIR / "eval_golden.jsonl"
TAXONOMY_PATH = DATASET_DIR / "failure_taxonomy.jsonl"

# 👎 原因 → 归因标签（关键词规则，人工复核时可直接改）
_TAXONOMY_RULES = (
    ("caliber", ("口径", "定义", "算法", "规则", "含", "不含")),
    ("time", ("时间", "日期", "昨天", "上周", "上月", "范围", "周期")),
    ("schema", ("字段", "表", "没有", "不存在", "缺少")),
    ("completeness", ("不完整", "不全", "少了", "漏")),
    ("performance", ("慢", "超时", "卡")),
    ("presentation", ("格式", "展示", "图", "排序")),
)


def infer_category(sql: str) -> str:
    """按 SQL 形状归类，保证 runner 的分层指标能统计到。"""
    try:
        parsed = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return "simple_agg"
    if any(parsed.find_all(exp.Join)):
        return "multi_join"
    for group in parsed.find_all(exp.Group):
        if any(c.name.lower() in ("dt", "date") for c in group.find_all(exp.Column)):
            return "time_range"
    return "simple_agg"


def infer_tables(sql: str) -> list[str]:
    try:
        parsed = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return []
    return sorted({t.name for t in parsed.find_all(exp.Table) if t.name})


def classify_negative(question: str, reason: str | None) -> str:
    """👎 归因：先看用户写的原因，再看问题文本，最后落 other。"""
    for text in (reason or "", question or ""):
        for label, words in _TAXONOMY_RULES:
            if any(w in text for w in words):
                return label
    return "other"


_CONTEXT_MARKER = "；本次补充（时间/条件优先于上句）："


def _context_list(value) -> list[str]:
    """question_context 入库为 JSON 数组字符串；老记录没有该列时返回空。"""
    if isinstance(value, list):
        items = value
    else:
        try:
            items = json.loads(value) if value else []
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    return [str(x).strip() for x in items if str(x).strip()]


def split_context(question: str, context=None) -> tuple[list[str], str]:
    """把一条记录拆成 (history, 本轮问题)。

    优先用入库的 question_context（完整前序提问，含中间轮次）；老记录只有拼接串时按标记拆。
    """
    prior = _context_list(context)
    if prior:
        return prior, (question or "").strip()
    q = (question or "").strip()
    if _CONTEXT_MARKER in q:
        prev, _, current = q.partition(_CONTEXT_MARKER)
        return ([prev.strip()] if prev.strip() else []), current.strip()
    return [], q


_ABS_DATE = re.compile(r"\d{4}[-/年]|\d{1,2}月\d{1,2}[日号]|\d{1,2}/\d{1,2}")
_REL_WORDS = re.compile(r"今天|今日|昨天|昨日|前天|本周|这周|上周|本月|这个月|上月|上个月|"
                        r"最近|近[一二三四五六七八九十0-9]|过去|今年|去年|本季度|上季度")


def is_relative(question: str) -> bool:
    """相对时间（昨天/上月/最近7天）会随数据日期漂移，不能作为稳定断言。"""
    q = question or ""
    return bool(_REL_WORDS.search(q)) and not bool(_ABS_DATE.search(q))


def build_case(record: dict, window, scalar, suspect: str | None) -> dict:
    """把一条 query_record 转成 runner 可识别的 case。"""
    history, question = split_context(record.get("question") or "", record.get("question_context"))
    sql = (record.get("generated_sql") or "").strip()
    # 只有绝对时间窗才做断言；相对时间/默认窗一律标 volatile，留给人工固定日期
    volatile = (window is None) or (not getattr(window, "explicit", True)) \
        or is_relative(" ".join(history + [question]))
    expect: dict = {}
    if not volatile and window is not None:
        expect["time_window"] = {"start": window.start.isoformat(),
                                 "end": window.end.isoformat()}
        if scalar is not None:
            expect["scalar"] = scalar
    notes = [f"harvested from query_record#{record.get('id')}",
             f"feedback={record.get('user_feedback') or 'none'}"]
    if record.get("feedback_reason"):
        notes.append(f"reason={record['feedback_reason']}")
    if suspect:
        notes.append(f"suspect={suspect}")
    if volatile:
        notes.append("volatile=相对时间，数据日期滚动后需重新 harvest")
    return {
        "case_id": f"H{record.get('id')}",
        "category": infer_category(sql),
        "difficulty": "medium",
        "question": question,
        **({"history": history} if history else {}),
        "clarify_rounds": 0,
        "expected_tables": infer_tables(sql),
        "expected_calibers": [],
        "expected_sql": sql,
        "expected_result_digest": str(scalar) if scalar is not None else f"{record.get('row_count')} 行",
        "reject_reason": None,
        "notes": " · ".join(notes),
        "expect": expect,
        "reviewed": False,
        "source": {"query_id": record.get("id"), "trace_id": record.get("trace_id"),
                   "feedback": record.get("user_feedback"), "suspect": suspect,
                   "volatile": volatile},
    }


def case_from_candidate(entry: dict) -> dict:
    """👍 候选池条目 → 草稿 case（无断言、默认 volatile，等人工复核补断言/固定日期）。"""
    history, question = split_context(entry.get("question") or "", entry.get("question_context"))
    sql = (entry.get("sql") or "").strip()
    return {
        "case_id": f"P{entry.get('query_id')}",
        "category": infer_category(sql) if sql else "unknown",
        "difficulty": "medium",
        "question": question,
        **({"history": history} if history else {}),
        "clarify_rounds": 0,
        "expected_tables": infer_tables(sql),
        "expected_calibers": [],
        "expected_sql": sql,
        "expected_result_digest": "",
        "reject_reason": None,
        "notes": f"👍 自动候选（feedback pool）· query_record#{entry.get('query_id')} · 待人工确认断言与固定日期",
        "expect": {},
        "reviewed": False,
        "source": {"query_id": entry.get("query_id"), "feedback": "up", "pool": True,
                   "volatile": True},
    }


def merge_pool(drafts: list[dict], entries: list[dict]) -> list[dict]:
    """把 👍 候选池并入草稿；按 (history, question) 去重，缺 SQL/问题的跳过。"""
    keys = {_case_key(c) for c in drafts}
    for entry in entries:
        if not (entry.get("sql") or "").strip() or not entry.get("question"):
            continue
        case = case_from_candidate(entry)
        key = _case_key(case)
        if key in keys:
            continue
        keys.add(key)
        drafts.append(case)
    return drafts


def _load_records(days: int, limit: int) -> list[dict]:
    from app.config import settings

    engine = sa.create_engine(settings.app_database_url)
    since = datetime.now() - timedelta(days=days)
    stmt = sa.text("SELECT id, session_id, question, question_context, generated_sql, row_count, latency_ms, "
                   "user_feedback, feedback_reason, trace_id, created_at "
                   "FROM query_record WHERE created_at >= :since "
                   # 有反馈的记录优先，避免同一问题的旧 👎 被无反馈的新记录挤掉
                   "ORDER BY (user_feedback IS NOT NULL) DESC, id DESC LIMIT :limit")
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"since": since, "limit": limit}).mappings().all()
    return [dict(r) for r in rows]


def _execute(sql: str, window):
    """重跑一次拿 golden 值；失败返回 (None, 错误信息)。"""
    from app.config import settings
    from app.services.executor import inject_dates
    from app.services.validator import validate_sql

    tables = set(infer_tables(sql))
    if not tables:
        return None, "no_table"
    v = validate_sql(sql, tables)
    if not v.passed:
        return None, "；".join(v.errors)[:120]
    engine = sa.create_engine(settings.data_source_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(sa.text(inject_dates(sql, window)))
            cols = list(result.keys())
            fetched = [dict(zip(cols, r)) for r in result.fetchmany(50)]
    except sa.exc.SQLAlchemyError as exc:
        return None, str(getattr(exc, "orig", exc)).splitlines()[0][:120]
    if len(cols) == 1 and len(fetched) == 1:
        value = fetched[0][cols[0]]
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, (int, float)):
            return value, None
        return None, None
    return None, None


def harvest(days: int = 30, limit: int = 200) -> dict:
    from app.core.time_window import resolve

    records = _load_records(days, limit)
    drafts, negatives, seen = [], [], set()
    stats = {"records": len(records), "drafted": 0, "negative": 0, "suspect": 0,
             "skipped": 0, "stable_candidates": 0}

    for rec in records:
        question = (rec.get("question") or "").strip()
        sql = (rec.get("generated_sql") or "").strip()
        if not question:
            stats["skipped"] += 1
            continue

        # 👎 一律进归因（含口径问答这类没有 SQL 的回答）
        if rec.get("user_feedback") == "down" and question not in seen:
            seen.add(question)
            stats["negative"] += 1
            negatives.append({
                "query_id": rec.get("id"), "question": question, "sql": sql or None,
                "reason": rec.get("feedback_reason"),
                "taxonomy": classify_negative(question, rec.get("feedback_reason")),
                "trace_id": rec.get("trace_id"),
            })
            continue

        if not sql or question in seen:
            stats["skipped"] += 1
            continue
        seen.add(question)

        # 时间窗按「本轮问题」解析；本轮没提时间则沿用前序提问（含入库的 question_context）
        history, current = split_context(question, rec.get("question_context"))
        window = resolve(current)
        if window is None:
            for prev_q in reversed(history):
                window = resolve(prev_q)
                if window:
                    break
        scalar, err = _execute(sql, window)
        suspect = err if err else None
        if suspect:
            stats["suspect"] += 1
        drafts.append(build_case(rec, window, scalar, suspect))

    # 同一会话重复提问会产生完全相同的 (history, question)，只保留最新一条
    drafts = _dedupe_cases(drafts)
    # D04：👍 反馈候选池并入草稿（去重后合并，人工复核前不带断言）
    try:
        from app.services.feedback_pool import feedback_pool
        drafts = _dedupe_cases(merge_pool(drafts, feedback_pool.read_all()))
    except Exception as exc:  # noqa: BLE001 —— 候选池损坏不能影响 harvest 主流程
        logger.warning("合并 👍 候选池失败：%s", exc)
    stats["drafted"] = len(drafts)
    stats["stable_candidates"] = sum(
        1 for c in drafts if not (c.get("source") or {}).get("suspect") and c.get("expect"))

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    drafts = carry_review(_read_jsonl(DRAFT_PATH), drafts)
    _write_jsonl(DRAFT_PATH, drafts)
    _write_jsonl(TAXONOMY_PATH, negatives)
    return stats


# 人工复核约定：备注以「复核：」开头；这三类断言只能人工补，重跑时要保留
_REVIEW_NOTE_PREFIX = "复核："
_MANUAL_EXPECT_KEYS = ("must_not", "min_rows", "assumption_contains")


def _case_key(case: dict) -> tuple:
    return tuple(case.get("history") or []), (case.get("question") or "").strip()


def _dedupe_cases(cases: list[dict]) -> list[dict]:
    """同一 (history, question) 只保留第一条（记录按 id 倒序进来，即最新一条）。"""
    unique, keys = [], set()
    for case in cases:
        key = _case_key(case)
        if key in keys:
            continue
        keys.add(key)
        unique.append(case)
    return unique


def carry_review(previous: list[dict], drafts: list[dict]) -> list[dict]:
    """重跑 harvest 时保留人工复核结果，避免草稿被整体覆盖后白复核一遍。

    按 (history, question) 对齐：沿用旧草稿的 reviewed、手工断言与「复核：」备注；
    时间窗/scalar 用本轮重跑的值（数据日期可能已滚动），不盲目沿用旧数值。
    """
    old = {_case_key(c): c for c in previous}
    for case in drafts:
        prev = old.get(_case_key(case))
        if not prev:
            continue
        if prev.get("reviewed") is True:
            case["reviewed"] = True
        prev_expect = prev.get("expect") or {}
        for key in _MANUAL_EXPECT_KEYS:
            if key in prev_expect:
                case["expect"][key] = prev_expect[key]
        kept = [p for p in str(prev.get("notes") or "").split(" · ")
                if p.startswith(_REVIEW_NOTE_PREFIX) and p not in case["notes"]]
        if kept:
            case["notes"] = " · ".join([case["notes"], *kept])
    return drafts


def _next_golden_seq(existing: list[dict]) -> int:
    """golden 编号顺延：从已有 Gxxx 的最大编号往后排，避免二次 promote 撞号。"""
    seqs = [int(m.group(1)) for c in existing
            if (m := re.match(r"^G(\d+)$", str(c.get("case_id") or "")))]
    return max(seqs, default=0) + 1


def promote() -> dict:
    """把人工确认过的草稿并入 eval_golden.jsonl（按问题去重）。"""
    if not DRAFT_PATH.exists():
        return {"promoted": 0, "message": "无 harvest_draft.jsonl，请先运行 harvest"}
    approved = [c for c in _read_jsonl(DRAFT_PATH)
                if c.get("reviewed") is True and not (c.get("source") or {}).get("suspect")]
    existing = _read_jsonl(GOLDEN_PATH)
    known = {(tuple(c.get("history") or []), c.get("question")) for c in existing}
    seq = _next_golden_seq(existing)
    added = 0
    for case in approved:
        key = (tuple(case.get("history") or []), case.get("question"))
        if key in known:
            continue
        added += 1
        case["case_id"] = f"G{seq:03d}"
        seq += 1
        existing.append(case)
        known.add(key)
    _write_jsonl(GOLDEN_PATH, existing)
    return {"promoted": added, "total": len(existing), "path": str(GOLDEN_PATH)}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
                    encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="历史问答转 golden 评测集")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--promote", action="store_true", help="把 reviewed=true 的草稿并入 eval_golden")
    args = parser.parse_args()

    if args.promote:
        print(json.dumps(promote(), ensure_ascii=False, indent=2))
        return
    stats = harvest(args.days, args.limit)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"草稿: {DRAFT_PATH}\n归因: {TAXONOMY_PATH}")


if __name__ == "__main__":
    main()
