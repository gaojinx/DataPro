"""把复核过的 golden 转成动态 few-shot 示例（A06）。

规则：
- 只收 `expected_sql` 里日期**已用占位符**或能从 `expect.time_window` 无损替换的用例
- 硬编码日期且无法替换的一律跳过（示例会污染全部生成）
- 输出 `data/fewshot_dynamic.jsonl`，字段与 `prompts/fewshot.json` 一致

用法：
  python -m eval.promote_fewshot --dry-run
  python -m eval.promote_fewshot
"""
import argparse
import json
from pathlib import Path

import sqlglot
from sqlglot import exp

DATASET_DIR = Path(__file__).parent / "datasets"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "fewshot_dynamic.jsonl"

DATE_COLUMNS = ("dt", "date", "day", "stat_date", "日期")


def _shape(sql: str, question: str) -> dict:
    """从 SQL 推断示例形状：单值 / 时间序列 / 维度分组 + 指标类型。"""
    from app.services.fewshot import metric_kind

    grain, dims = "total", 0
    try:
        stmt = sqlglot.parse_one(sql)
        group = stmt.args.get("group")
        group_cols = [g.name.lower() for g in group.expressions if isinstance(g, exp.Column)] \
            if group else []
        if group_cols:
            date_like = [c for c in group_cols if any(d in c for d in DATE_COLUMNS)]
            grain = "time" if date_like else "dim"
            dims = max(len(group_cols) - len(date_like), 1)
    except Exception:  # noqa: BLE001 —— 解析失败按单值处理
        pass
    return {"grain": grain, "metric": metric_kind(question), "dims": dims}


def to_example(case: dict) -> dict | None:
    sql = (case.get("expected_sql") or "").strip()
    question = (case.get("question") or "").strip()
    if not sql or not question:
        return None
    start = ((case.get("expect") or {}).get("time_window") or {}).get("start")
    end = ((case.get("expect") or {}).get("time_window") or {}).get("end")
    if "{date_start}" not in sql:
        if not (start and end and f"'{start}'" in sql and f"'{end}'" in sql):
            return None                    # 硬编码日期且无法替换 → 跳过
        sql = sql.replace(f"'{start}'", "'{date_start}'").replace(f"'{end}'", "'{date_end}'")
    example = {"question": question, "sql": sql,
               "tables": case.get("expected_tables") or [],
               "case_id": case.get("case_id")}
    example.update(_shape(sql, question))
    return example


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default="eval_golden")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = DATASET_DIR / f"{args.golden}.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    examples, skipped = [], []
    for case in cases:
        ex = to_example(case)
        (examples if ex else skipped).append(ex or case.get("case_id"))

    print(f"可用示例 {len(examples)} 条，跳过 {len(skipped)} 条：{skipped}")
    if args.dry_run:
        for ex in examples:
            print("-", ex["case_id"], ex["grain"], ex["metric"], ex["question"])
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in examples),
                   encoding="utf-8")
    print("已写入", out)


if __name__ == "__main__":
    main()
