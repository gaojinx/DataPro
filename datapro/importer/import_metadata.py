"""元数据/口径导入（D05）：填报模板 → MySQL `meta_table` / `meta_column` / `metric_caliber`。

用法：
  python -m importer.import_metadata --file 元数据.xlsx --dry-run
  python -m importer.import_metadata --file 元数据.xlsx
  python -m importer.import_metadata --file calibers.csv --kind calibers

XLSX 按工作表名识别：含「字段/column」→ 字段级，含「口径/caliber」→ 口径库，其余含「表/table」→ 表级。
导入后调用 `metadata_store.invalidate()`，召回下一次请求即生效。
"""
import argparse
import csv
from pathlib import Path

# 模板英文列名 + 常见中文表头别名
ALIASES = {
    "库名": "db_name", "表名": "table_name", "中文业务名": "cn_name", "数据粒度": "granularity",
    "更新频率": "update_freq", "业务负责人": "owner", "数据分级": "dt_level", "业务场景说明": "description",
    "字段名": "col_name", "中文名": "cn_name", "类型": "data_type", "枚举值含义": "enum_meaning",
    "关联口径": "caliber_id", "是否敏感": "is_pii", "样例值": "sample_values", "常见误用": "common_pitfalls",
    "指标编号": "metric_id", "指标名": "metric_name", "常见叫法": "aliases", "定义": "definition",
    "计算逻辑": "logic", "来源表": "source_table", "常用维度": "dimensions", "状态": "status",
    "选择关键词": "choice_keywords",
}

KIND_FIELDS = {
    "tables": {"db_name", "table_name", "cn_name"},
    "columns": {"table_name", "col_name"},
    "calibers": {"metric_id", "metric_name", "definition", "logic"},
}


def _clean(v) -> str:
    return "" if v is None else str(v).strip()


def _norm_header(header: str) -> str:
    key = _clean(header).lower()
    return ALIASES.get(key, key)


def _rows_to_dicts(header: list[str], rows: list[list]) -> list[dict]:
    cols = [_norm_header(h) for h in header]
    out = []
    for raw in rows:
        item = {}
        for i, col in enumerate(cols):
            if not col or i >= len(raw):
                continue
            val = _clean(raw[i])
            if val:
                item[col] = val
        if item:
            out.append(item)
    return out


def parse_csv(path: Path, kind: str) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = list(csv.reader(f))
    if not reader:
        return []
    return _rows_to_dicts(reader[0], reader[1:])


def parse_xlsx(path: Path) -> dict[str, list[dict]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out: dict[str, list[dict]] = {"tables": [], "columns": [], "calibers": []}
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if len(rows) < 2:
            continue
        name = (ws.title or "").lower()
        if "字段" in name or "column" in name:
            kind = "columns"
        elif "口径" in name or "caliber" in name:
            kind = "calibers"
        else:
            kind = "tables"
        out[kind] += _rows_to_dicts([_clean(c) for c in rows[0]], rows[1:])
    return out


def normalize(kind: str, rows: list[dict]) -> tuple[list[dict], list[str]]:
    """校验必填列并转成入库结构；返回（有效行, 错误说明）。"""
    required = KIND_FIELDS[kind]
    valid, errors = [], []
    for i, row in enumerate(rows, start=2):
        row = {_norm_header(k): v for k, v in row.items()}   # 兼容中文表头直接传入
        missing = required - set(row)
        if missing:
            errors.append(f"第 {i} 行缺少必填列：{', '.join(sorted(missing))}")
            continue
        if kind == "tables":
            row = dict(row)
            row["keywords"] = ",".join(k.strip() for k in row.get("keywords", "").replace("，", ",").split(",")
                                       if k.strip())
            row.setdefault("description", row.get("cn_name", ""))
        elif kind == "calibers":
            row = dict(row)
            row["aliases"] = ",".join(k.strip() for k in row.get("aliases", "").replace("，", ",").split(",")
                                      if k.strip())
            row["choice_keywords"] = ",".join(
                k.strip() for k in row.get("choice_keywords", "").replace("，", ",").split(",") if k.strip())
            if row.get("dimensions") and not row.get("source_table"):
                row["source_table"] = ""
        elif kind == "columns":
            row = dict(row)
            row["is_pii"] = str(row.get("is_pii", "")).lower() in ("1", "true", "是", "y", "yes")
        valid.append(row)
    return valid, errors


def upsert(kind: str, rows: list[dict]) -> tuple[int, int]:
    """按业务主键 upsert，返回（新增, 更新）。口径变更会留历史快照。"""
    from app.db.models import MetaColumn, MetaTable, MetricCaliber
    from app.db.session import SessionLocal
    from app.services.caliber_history import snapshot

    is_caliber = kind not in ("tables", "columns")
    added = updated = 0
    with SessionLocal() as db:
        for row in rows:
            if kind == "tables":
                obj = db.query(MetaTable).filter_by(table_name=row["table_name"]).one_or_none()
                model, key = MetaTable, {"table_name": row["table_name"]}
            elif kind == "columns":
                obj = db.query(MetaColumn).filter_by(table_name=row["table_name"],
                                                     col_name=row["col_name"]).one_or_none()
                model, key = MetaColumn, {"table_name": row["table_name"], "col_name": row["col_name"]}
            else:
                obj = db.query(MetricCaliber).filter_by(metric_id=row["metric_id"]).one_or_none()
                model, key = MetricCaliber, {"metric_id": row["metric_id"]}
            if obj is None:
                # 模板里的“常用维度”等说明列可能不是模型字段，入库前过滤，避免 TypeError
                payload = {k: v for k, v in {**row, **key}.items() if hasattr(model, k)}
                if hasattr(model, "owner") and not payload.get("owner"):
                    payload["owner"] = ""
                obj = model(**payload)
                db.add(obj)
                if is_caliber:
                    db.flush()                       # 拿到落库后的完整字段再留档
                    snapshot(db, obj, "create", "importer")
                added += 1
            else:
                if is_caliber:
                    # 修改前快照：历史表记录的是「改之前是什么」，便于回溯与回滚
                    snapshot(db, obj, "update", "importer")
                for k, v in row.items():
                    if hasattr(obj, k) and v != "":
                        setattr(obj, k, v)
                updated += 1
        db.commit()
    return added, updated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="XLSX 填报模板，或 CSV（需 --kind）")
    ap.add_argument("--kind", choices=list(KIND_FIELDS), help="CSV 文件的数据类型")
    ap.add_argument("--dry-run", action="store_true", help="只解析与校验，不写库")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    if path.suffix.lower() == ".csv":
        if not args.kind:
            raise SystemExit("CSV 需要指定 --kind tables|columns|calibers")
        batches = {args.kind: parse_csv(path, args.kind)}
    else:
        batches = parse_xlsx(path)

    total = 0
    for kind, rows in batches.items():
        valid, errors = normalize(kind, rows)
        print(f"[{kind}] 解析 {len(rows)} 行，有效 {len(valid)} 行")
        for e in errors[:10]:
            print("  -", e)
        if args.dry_run:
            if valid:
                print("  样例：", valid[0])
            continue
        if valid:
            added, updated = upsert(kind, valid)
            total += added + updated
            print(f"  写库完成：新增 {added}，更新 {updated}")
    if args.dry_run:
        print("dry-run：未写库")
    else:
        from app.services.metadata_store import metadata_store
        metadata_store.invalidate()
        print(f"导入完成，共 {total} 行；召回缓存已刷新")


if __name__ == "__main__":
    main()
