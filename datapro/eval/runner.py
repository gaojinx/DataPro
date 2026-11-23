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

DATASET_DIR = Path(__file__).parent / "datasets"
QUERY_CATEGORIES = {"simple_agg", "multi_join", "time_range"}


async def run_evaluation(dataset: str = "eval_v1_seed") -> dict:
    from app.api.routes_chat import build_pipeline
    from app.core.statemachine import PipelineContext

    pipeline = build_pipeline()
    cases = [json.loads(line) for line in
             (DATASET_DIR / f"{dataset}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    results = []
    for case in cases:
        ctx = PipelineContext(question=case["question"], user_id="eval_bot")
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

        entry = {
            "case_id": case["case_id"],
            "category": case["category"],
            "difficulty": case.get("difficulty"),
            "question": case["question"],
            "outcome": outcome,
            "events": types,
            "table_hit": table_hit if expected_tables else None,
            "sql": (res or {}).get("sql"),
            "row_count": (res or {}).get("row_count"),
            "mode": "degraded" if res and any("未配置 LLM" in a for a in res.get("assumptions", []))
                    else ("llm" if res else None),
        }

        category = case["category"]
        if category == "reject_expected":
            entry["pass"] = bool(rej) and rej.get("reason_code") == case.get("reject_reason")
        elif category == "metadata_qa":
            entry["pass"] = bool(res) and bool(res.get("summary"))
        elif category == "clarify_needed":
            entry["pass"] = "clarify" in types or "result" in types
        else:  # 取数类
            entry["pass"] = bool(res) and bool(res.get("sql"))
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

    summary = {
        "dataset": dataset,
        "total": len(results),
        "metrics": {
            "executable_rate": rate(query_items),                     # 取数类可执行率（G2 ≥95%）
            "overall_pass_rate": rate(results),
            "table_hit_rate": rate([{**r, "pass": r["table_hit"]} for r in hit_items]),
            "llm_mode_cases": sum(1 for r in results if r["mode"] == "llm"),
            "degraded_cases": sum(1 for r in results if r["mode"] == "degraded"),
        },
        "by_category": by_cat,
        "failure_breakdown": dict(Counter(r["outcome"] for r in results if not r["pass"])),
        "passed": sum(1 for r in results if r["pass"]),
        "details": results,
    }
    report_path = DATASET_DIR.parent / "reports" / f"{dataset}_report.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
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
