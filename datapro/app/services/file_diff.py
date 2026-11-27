"""文件版本对比（F49 扩展）：CSV/TSV/XLSX 的单元格级 diff。

对齐策略：
- 工作表按名称对齐（同名对比；新增/删除的表单独标注）
- 行用 difflib.SequenceMatcher 对齐，能识别插入/删除行，避免整表错位
- 表头单独对比，数据行再逐单元格对比
- 输出限制每个工作表 300 处变更、总计 1000 处，超出标记 truncated
"""
from __future__ import annotations

import difflib

from app.services.file_ingest import _read_delimited, _read_xlsx
from app.services.file_store import file_store

MAX_CELL_CHANGES = 300
MAX_TOTAL_CHANGES = 1000
SUPPORTED_EXTS = (".csv", ".tsv", ".xlsx")


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _col_letter(index: int) -> str:
    """0-based 列号转 Excel 列名：0→A, 26→AA。"""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def load_datasets(entry: dict):
    """读取文件为 [(sheet_name, header, rows), ...]。"""
    ext = (entry.get("ext") or "").lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError("仅支持 CSV / TSV / XLSX 文件对比")
    raw = file_store.content_of(entry)
    if ext == ".xlsx":
        return _read_xlsx(raw)
    return [_read_delimited(raw, "\t" if ext == ".tsv" else ",")]


def diff_datasets(before: list, after: list) -> dict:
    """返回结构化 diff；before/after 为 load_datasets 的结果。"""
    before_map = {name or f"Sheet{i + 1}": (header, rows)
                  for i, (name, header, rows) in enumerate(before)}
    after_map = {name or f"Sheet{i + 1}": (header, rows)
                 for i, (name, header, rows) in enumerate(after)}

    sheets = []
    total_changes = 0
    total_added = total_removed = total_changed = 0
    truncated_any = False

    for name in list(after_map.keys()) + [n for n in before_map if n not in after_map]:
        b = before_map.get(name)
        a = after_map.get(name)
        if b is None:
            sheets.append({"name": name, "status": "added",
                           "added_rows": len(a[1]), "removed_rows": 0,
                           "changed_cells": 0, "header_changes": [], "changes": []})
            total_added += len(a[1])
            continue
        if a is None:
            sheets.append({"name": name, "status": "removed",
                           "added_rows": 0, "removed_rows": len(b[1]),
                           "changed_cells": 0, "header_changes": [], "changes": []})
            total_removed += len(b[1])
            continue

        b_header, b_rows = b
        a_header, a_rows = a
        header_changes = []
        for col in range(max(len(b_header), len(a_header))):
            bv = _norm(b_header[col]) if col < len(b_header) else ""
            av = _norm(a_header[col]) if col < len(a_header) else ""
            if bv != av:
                header_changes.append({"col": _col_letter(col), "before": bv, "after": av})

        b_norm = [tuple(_norm(c) for c in row) for row in b_rows]
        a_norm = [tuple(_norm(c) for c in row) for row in a_rows]
        matcher = difflib.SequenceMatcher(None, b_norm, a_norm, autojunk=False)
        changes, added_rows, removed_rows = [], 0, 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "insert":
                added_rows += j2 - j1
                continue
            if tag == "delete":
                removed_rows += i2 - i1
                continue
            # replace：逐行逐单元格比较，多出的行记为新增/删除
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                before_row, after_row = b_norm[i1 + k], a_norm[j1 + k]
                for col in range(max(len(before_row), len(after_row))):
                    bv = before_row[col] if col < len(before_row) else ""
                    av = after_row[col] if col < len(after_row) else ""
                    if bv == av:
                        continue
                    if len(changes) < MAX_CELL_CHANGES and total_changes < MAX_TOTAL_CHANGES:
                        changes.append({"row": j1 + k + 1, "col": _col_letter(col),
                                        "before": bv, "after": av})
                        total_changes += 1
                        total_changed += 1
                    else:
                        truncated_any = True
            if j2 - j1 > pairs:
                added_rows += (j2 - j1) - pairs
            elif i2 - i1 > pairs:
                removed_rows += (i2 - i1) - pairs

        status = "same"
        if header_changes or changes or added_rows or removed_rows:
            status = "modified"
        total_added += added_rows
        total_removed += removed_rows
        sheets.append({
            "name": name, "status": status,
            "before_rows": len(b_rows), "after_rows": len(a_rows),
            "added_rows": added_rows, "removed_rows": removed_rows,
            "changed_cells": len(changes), "header_changes": header_changes,
            "changes": changes,
        })

    return {
        "summary": {
            "sheets": len(sheets),
            "added_rows": total_added,
            "removed_rows": total_removed,
            "changed_cells": total_changed,
            "header_changes": sum(len(s["header_changes"]) for s in sheets),
            "truncated": truncated_any,
        },
        "sheets": sheets,
    }


def compare(file_id: str, against_id: str) -> dict:
    """对比两个已上传文件；返回包含两侧文件元信息的 diff。"""
    current = file_store.get(file_id)
    base = file_store.get(against_id)
    if not current or not base:
        raise ValueError("文件不存在")
    before = load_datasets(base)
    after = load_datasets(current)
    result = diff_datasets(before, after)
    result["file"] = {k: current.get(k) for k in ("file_id", "name", "version")}
    result["against"] = {k: base.get(k) for k in ("file_id", "name", "version")}
    return result
