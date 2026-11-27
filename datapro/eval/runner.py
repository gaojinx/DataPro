"""评测回归 runner（评测集规范 §2）。

用法：python -m eval.runner [dataset_name] [--verbose]
产出：逐 case 结果 + 分类指标（可执行率/拒答准确率/口径问答率/表命中率）+ 失败归因。
无 LLM Key 时条目会标记 mode=degraded（口径模板降级），与真实 LLM 生成区分统计。
"""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import exp

DATASET_DIR = Path(__file__).parent / "datasets"
QUERY_CATEGORIES = {"simple_agg", "multi_join", "time_range"}


def sql_tables(sql: str) -> set[str]:
    """SQL 里引用的表名（小写）；解析失败返回空集合。"""
    if not sql:
        return set()
    try:
        return {t.name.lower() for stmt in sqlglot.parse(sql) for t in stmt.find_all(exp.Table)
                if t.name}
    except Exception:  # noqa: BLE001 —— 评测统计不能因脏 SQL 中断
        return set()


def _single_value(res: dict | None):
    """单值聚合结果（1 列 1 行）。"""
    if not res:
        return None
    cols = res.get("columns") or []
    rows = res.get("rows") or []
    if len(cols) == 1 and len(rows) == 1:
        return rows[0].get(cols[0])
    return None


def _num_equal(a, b, tol: float = 0.01) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


async def run_evaluation(dataset: str = "eval_v1_seed") -> dict:
    from app.api.routes_chat import build_pipeline
    from app.core.statemachine import PipelineContext

    pipeline = build_pipeline()
    cases = [json.loads(line) for line in
             (DATASET_DIR / f"{dataset}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    results = []
    for case in cases:
        # 与线上一致：history 末尾是当前问题（前序轮次用于槽位继承/指代消解）
        priors = [{"role": "user", "content": h} for h in (case.get("history") or [])]
        history = priors + [{"role": "user", "content": case["question"]}]
        ctx = PipelineContext(question=case["question"], user_id="eval_bot", history=history)
        events = [e async for e in pipeline.run(ctx)]
        types = [e.get("type") for e in events]

        res = next((e for e in events if e.get("type") == "result"), None)
        rej = next((e for e in events if e.get("type") == "reject"), None)
        err = next((e for e in events if e.get("type") == "error"), None)
        esc = next((e for e in events if e.get("type") == "escalate"), None)
        if esc:
            outcome = "escalate"
        elif err:
            outcome = f"error:{err.get('reason_code')}"
        elif rej:
            outcome = f"reject:{rej.get('reason_code')}"
        elif "clarify" in types:
            outcome = "clarify"
        elif res:
            outcome = "result"
        else:
            outcome = "unknown"

        expected_tables = case.get("expected_tables") or []
        table_hit = bool(expected_tables) and all(t in ctx.recalled_tables for t in expected_tables)

        expect = case.get("expect") or {}
        window = getattr(ctx, "time_window", None)
        time_window = {"start": window.start.isoformat(), "end": window.end.isoformat()} if window else None
        exp_window = expect.get("time_window")
        time_ok = None
        if exp_window and time_window:
            time_ok = (time_window["start"] == exp_window.get("start")
                       and time_window["end"] == exp_window.get("end"))
        scalar = _single_value(res)
        exp_scalar = expect.get("scalar")
        result_ok = _num_equal(scalar, exp_scalar) if exp_scalar is not None else None
        must_not = expect.get("must_not") or []
        sql_text = (res or {}).get("sql") or ""
        must_not_ok = None
        if must_not:
            must_not_ok = not any(token in sql_text for token in must_not)
        note_kw = expect.get("assumption_contains")
        note_ok = None
        if note_kw and res is not None:
            note_ok = any(note_kw in a for a in (res.get("assumptions") or []))
        min_rows = expect.get("min_rows")
        rows_ok = None
        if min_rows is not None:
            rows_ok = (res or {}).get("row_count", 0) >= min_rows
        spec = getattr(ctx, "spec", None)
        spec_dict = spec.to_dict() if spec else None

        entry = {
            "case_id": case["case_id"],
            "category": case["category"],
            "difficulty": case.get("difficulty"),
            "question": case["question"],
            "outcome": outcome,
            "events": types,
            "table_hit": table_hit if expected_tables else None,
            "time_window": time_window,
            "time_window_pass": time_ok,
            "scalar": scalar,
            "result_pass": result_ok,
            "must_not_pass": must_not_ok,
            "note_pass": note_ok,
            "rows_pass": rows_ok,
            "spec": spec_dict,
            "sql": (res or {}).get("sql"),
            "row_count": (res or {}).get("row_count"),
            "mode": "degraded" if res and any("未配置 LLM" in a for a in res.get("assumptions", []))
                    else ("llm" if res else None),
        }

        category = case["category"]
        if category == "reject_expected":
            entry["pass"] = bool(rej) and rej.get("reason_code") == case.get("reject_reason")
        elif category == "out_of_scope":
            # 召回 0 命中：有模型时由模型说明可查询范围（0 行、不含数值）
            entry["pass"] = bool(res) and res.get("row_count") == 0 and not res.get("sql")
        elif category == "metadata_qa":
            entry["pass"] = bool(res) and bool(res.get("summary"))
        elif category == "clarify_needed":
            entry["pass"] = "clarify" in types or "result" in types
        else:  # 取数类：可执行 + 时间窗正确 + 数值正确（有 golden 时）
            entry["pass"] = (bool(res) and bool(res.get("sql"))
                             and time_ok is not False and result_ok is not False
                             and must_not_ok is not False and note_ok is not False
                             and rows_ok is not False)
        results.append(entry)

    def rate(items):
        return round(sum(1 for r in items if r["pass"]) / len(items), 3) if items else 0.0

    by_cat = {}
    for cat in sorted({c["category"] for c in cases}):
        items = [r for r in results if r["category"] == cat]
        by_cat[cat] = {"total": len(items), "passed": sum(1 for r in items if r["pass"]),
                       "rate": rate(items)}

    query_items = [r for r in results if r["category"] in QUERY_CATEGORIES]
    hit_items = [r for r in query_items if r["table_hit"] is not None]
    time_items = [r for r in results if r["time_window_pass"] is not None]
    golden_items = [r for r in results if r["result_pass"] is not None]
    wrong_items = [r for r in golden_items if r["result_pass"] is False and r["sql"]]
    must_not_items = [r for r in results if r["must_not_pass"] is not None]
    # E05 幻觉专项：SQL 引用了期望之外的表（越界取数/臆造表）；仅在有用例期望的场景统计
    overflow = []
    for r in results:
        expected = {t.lower() for t in (r.get("expected_tables") or [])}
        extra = sql_tables(r.get("sql") or "") - expected if expected else set()
        if extra:
            overflow.append({"case_id": r["case_id"], "extra_tables": sorted(extra)})

    def rate_of(items, key):
        return round(sum(1 for r in items if r[key]) / len(items), 3) if items else None

    summary = {
        "dataset": dataset,
        "total": len(results),
        "metrics": {
            "executable_rate": rate(query_items),                     # 取数类可执行率（G2 ≥95%）
            "overall_pass_rate": rate(results),
            "table_hit_rate": rate([{**r, "pass": r["table_hit"]} for r in hit_items]),
            "time_window_pass_rate": rate_of(time_items, "time_window_pass"),   # 时间窗是否与期望一致
            "result_match_rate": rate_of(golden_items, "result_pass"),          # 有 golden 的数值命中率
            "false_answer_rate": (round(len(wrong_items) / len(golden_items), 3)
                                  if golden_items else None),                   # 有数但数值错（必须为 0）
            "must_not_pass_rate": rate_of(must_not_items, "must_not_pass"),
            "table_overflow_rate": (round(len(overflow) / len(hit_items), 3)
                                    if hit_items else None),                 # E05：越界取数占比
            "llm_mode_cases": sum(1 for r in results if r["mode"] == "llm"),
            "degraded_cases": sum(1 for r in results if r["mode"] == "degraded"),
        },
        "by_category": by_cat,
        "failure_breakdown": dict(Counter(r["outcome"] for r in results if not r["pass"])),
        "table_overflow": overflow,
        "passed": sum(1 for r in results if r["pass"]),
        "details": results,
    }
    report_path = DATASET_DIR.parent / "reports" / f"{dataset}_report.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
    return summary


if __name__ == "__main__":
    name = next((a for a in sys.argv[1:] if not a.startswith("--")), "eval_v1_seed")
    verbose = "--verbose" in sys.argv
    report = asyncio.run(run_evaluation(name))
    if verbose:
        for r in report["details"]:
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"  [{mark}] {r['case_id']} {r['category']:<16} {r['outcome']:<14} "
                  f"{'表命中' if r['table_hit'] else ''} {r['question']}")
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=2))
