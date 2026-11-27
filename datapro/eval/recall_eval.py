"""召回质量评测（A05 验收）：用种子集期望表/口径衡量关键词路与向量路的命中率。

指标：
- table_hit_rate：期望表全部被召回（all-hit）
- caliber_hit_rate：期望口径至少命中一个
- miss 明细：未命中的用例与缺失表/口径，便于补关键词或调向量阈值

用法：
  python -m eval.recall_eval                       # 当前配置（默认关键词路）
  python -m eval.recall_eval --dataset eval_golden
  python -m eval.recall_eval --out eval/reports/recall_report.json
"""
import argparse
import asyncio
import json
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "datasets"


def load_cases(dataset: str) -> list[dict]:
    path = DATASET_DIR / f"{dataset}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def evaluate(cases: list[dict], recall_fn) -> dict:
    """recall_fn(question) -> (tables:list[dict], calibers:list[dict])。"""
    rows, misses = [], []
    for case in cases:
        expected_tables = [t.lower() for t in case.get("expected_tables") or []]
        expected_calibers = case.get("expected_calibers") or []
        if not expected_tables and not expected_calibers:
            continue
        tables, calibers = recall_fn(case["question"])
        got_tables = {t.get("table_name", "").lower() for t in tables}
        got_calibers = {c.get("metric_id") for c in calibers}
        table_ok = (set(expected_tables) <= got_tables) if expected_tables else None
        caliber_ok = bool(set(expected_calibers) & got_calibers) if expected_calibers else None
        rows.append({"case_id": case.get("case_id"), "category": case.get("category"),
                     "table_ok": table_ok, "caliber_ok": caliber_ok})
        if expected_tables and not table_ok:
            misses.append({"case_id": case.get("case_id"), "question": case["question"],
                           "missing_tables": sorted(set(expected_tables) - got_tables)})
        if expected_calibers and not caliber_ok:
            misses.append({"case_id": case.get("case_id"), "question": case["question"],
                           "missing_calibers": expected_calibers,
                           "got_calibers": sorted(c for c in got_calibers if c)})

    table_rows = [r for r in rows if r["table_ok"] is not None]
    caliber_rows = [r for r in rows if r["caliber_ok"] is not None]
    table_total = sum(1 for c in cases if c.get("expected_tables"))
    caliber_total = sum(1 for c in cases if c.get("expected_calibers"))
    return {
        "cases": len(cases),
        "table_cases": table_total,
        "table_hits": sum(1 for r in table_rows if r["table_ok"]),
        "table_hit_rate": round(sum(1 for r in table_rows if r["table_ok"]) / table_total, 4)
        if table_total else None,
        "caliber_cases": caliber_total,
        "caliber_hits": sum(1 for r in caliber_rows if r["caliber_ok"]),
        "caliber_hit_rate": round(sum(1 for r in caliber_rows if r["caliber_ok"]) / caliber_total, 4)
        if caliber_total else None,
        "by_category": _by_category(rows),
        "misses": misses,
    }


def _by_category(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        cat = r["category"] or "unknown"
        item = out.setdefault(cat, {"cases": 0, "table_cases": 0, "table_hits": 0,
                                    "caliber_cases": 0, "caliber_hits": 0})
        item["cases"] += 1
        if r["table_ok"] is not None:
            item["table_cases"] += 1
            item["table_hits"] += int(bool(r["table_ok"]))
        if r["caliber_ok"] is not None:
            item["caliber_cases"] += 1
            item["caliber_hits"] += int(bool(r["caliber_ok"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="eval_v1_seed")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from app.services.recall import recall_service

    cases = load_cases(args.dataset)

    def recall_fn(question: str):
        result = asyncio.run(recall_service.recall(question))
        return result.tables, result.calibers

    report = evaluate(cases, recall_fn)
    print(json.dumps({k: v for k, v in report.items() if k not in ("by_category", "misses")},
                     ensure_ascii=False, indent=2))
    print("分类：", json.dumps(report["by_category"], ensure_ascii=False))
    if report["misses"]:
        print("未命中：")
        for m in report["misses"]:
            print(" -", m)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("报告已写入", path)


if __name__ == "__main__":
    main()
