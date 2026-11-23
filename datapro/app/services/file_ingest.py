"""上传文件解析入库（F27）。

支持格式：CSV / TSV / JSON（对象数组）/ XLSX（全部工作表，各建一张表）。

设计取舍：
- 列名统一映射为 c1..cn（ASCII），原始表头存 labels 注入 prompt，
  避免中文列名在 SQL 解析/反引号处理上的问题
- 单表上限：2 万行 × 50 列；XLSX 最多 10 个工作表（超限拒绝）
- 表名由 file_id(hex) 生成：首个工作表 f_{file_id}，其余 f_{file_id}_s2、_s3…
- 每个会话一个只读数据库账号，仅授权本会话的表（见 db_identity）
"""
import csv
import io
import json
import logging

import sqlalchemy as sa

from app.config import settings
from app.services.db_identity import (TEMP_DB, ensure_session_user, revoke_session_table,
                                      revoke_temp_db_from_shared_account)
from app.services.file_store import file_store
from app.services.session_registry import session_registry

logger = logging.getLogger("datapro.ingest")

MAX_ROWS = settings.upload_max_rows
MAX_COLS = settings.upload_max_cols
MAX_SHEETS = 10
MAX_CELL = 1024

Dataset = tuple[str, list[str], list[list]]  # (sheet_name, header, rows)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8")


def _read_delimited(raw: bytes, delimiter: str) -> Dataset:
    reader = csv.reader(io.StringIO(_decode(raw)), delimiter=delimiter)
    rows = [r for r in reader if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError("文件为空")
    return "", rows[0], rows[1:]


def _read_json(raw: bytes) -> Dataset:
    data = json.loads(_decode(raw))
    if isinstance(data, dict):
        # 兼容 {"data": [...]} / {"list": [...]} 这类包裹结构
        wrapped = next((v for v in data.values()
                        if isinstance(v, list) and v and isinstance(v[0], dict)), None)
        data = wrapped if wrapped is not None else [data]
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("JSON 需为对象数组，例如 [{\"a\":1}, {\"a\":2}]")
    header = list(data[0].keys())
    rows = [[_json_cell(item.get(h)) for h in header] for item in data]
    return "", header, rows


def _json_cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:MAX_CELL]
    return value


def _read_xlsx(raw: bytes) -> list[Dataset]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    datasets: list[Dataset] = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)
                if any(v is not None and str(v).strip() != "" for v in r)]
        if not rows:
            continue
        header = [str(c) if c is not None else f"列{i + 1}" for i, c in enumerate(rows[0])]
        datasets.append((ws.title, header, rows[1:]))
    wb.close()
    if not datasets:
        raise ValueError("工作簿为空")
    return datasets


def _infer_type(values: list) -> str:
    sample = [v for v in values if v not in (None, "")][:200]
    if not sample:
        return "VARCHAR(512)"
    try:
        for v in sample:
            int(str(v))
        return "BIGINT"
    except (ValueError, TypeError):
        pass
    try:
        for v in sample:
            float(str(v))
        return "DOUBLE"
    except (ValueError, TypeError):
        return "VARCHAR(512)"


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_CELL]
    return value


def table_names_of(parsed: dict | None) -> list[str]:
    """兼容单表（旧结构）与多表结构。"""
    if not parsed:
        return []
    if parsed.get("tables"):
        return [t["table"] for t in parsed["tables"]]
    return [parsed["table"]] if parsed.get("table") else []


class FileIngestService:
    def _engine(self) -> sa.Engine:
        return sa.create_engine(settings.upload_db_url, pool_pre_ping=True)

    def _read(self, ext: str, raw: bytes) -> list[Dataset]:
        if ext == ".csv":
            return [_read_delimited(raw, ",")]
        if ext == ".tsv":
            return [_read_delimited(raw, "\t")]
        if ext == ".json":
            return [_read_json(raw)]
        if ext == ".xlsx":
            return _read_xlsx(raw)
        raise ValueError("暂仅支持 CSV / TSV / JSON / XLSX 解析")

    def ingest(self, file_id: str) -> dict:
        entry = file_store.get(file_id)
        if not entry:
            raise ValueError("文件不存在")
        raw = file_store.path_of(entry).read_bytes()
        datasets = self._read(entry["ext"], raw)
        if len(datasets) > MAX_SHEETS:
            raise ValueError(f"工作表数量 {len(datasets)} 超过上限 {MAX_SHEETS}")

        total_rows = 0
        prepared = []
        for idx, (sheet, header, data) in enumerate(datasets):
            if len(header) > MAX_COLS:
                raise ValueError(f"工作表「{sheet or '默认'}」列数 {len(header)} 超过上限 {MAX_COLS}")
            if len(data) > MAX_ROWS:
                raise ValueError(f"工作表「{sheet or '默认'}」行数 {len(data)} 超过上限 {MAX_ROWS}")
            table = f"f_{file_id}" if idx == 0 else f"f_{file_id}_s{idx + 1}"
            cols = [f"c{i + 1}" for i in range(len(header))]
            types = [_infer_type([r[i] if i < len(r) else None for r in data])
                     for i in range(len(cols))]
            prepared.append({"table": table, "sheet": sheet, "header": header,
                             "cols": cols, "types": types, "data": data})
            total_rows += len(data)

        session_id = entry["session_id"]
        tables_meta = []
        eng = self._engine()
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sa.text("CREATE DATABASE IF NOT EXISTS dp_uploads "
                                 "DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci"))
            for p in prepared:
                defs = ", ".join(f"`{c}` {t}" for c, t in zip(p["cols"], p["types"]))
                conn.execute(sa.text(f"DROP TABLE IF EXISTS `{TEMP_DB}`.`{p['table']}`"))
                conn.execute(sa.text(f"CREATE TABLE `{TEMP_DB}`.`{p['table']}` ({defs}) "
                                     f"ENGINE=InnoDB DEFAULT CHARSET utf8mb4"))
                insert_sql = sa.text(
                    f"INSERT INTO `{TEMP_DB}`.`{p['table']}` "
                    f"({', '.join(f'`{c}`' for c in p['cols'])}) "
                    f"VALUES ({', '.join(f':{c}' for c in p['cols'])})")
                batch = []
                for r in p["data"]:
                    batch.append({c: _clean(r[i] if i < len(r) else None)
                                  for i, c in enumerate(p["cols"])})
                    if len(batch) >= 500:
                        conn.execute(insert_sql, batch)
                        batch = []
                if batch:
                    conn.execute(insert_sql, batch)
                tables_meta.append({
                    "table": p["table"], "schema": TEMP_DB, "sheet": p["sheet"],
                    "columns": p["cols"], "labels": p["header"],
                    "row_count": len(p["data"]), "col_count": len(p["cols"]),
                })
            ensure_session_user(conn, session_id, tables=[t["table"] for t in tables_meta])
            revoke_temp_db_from_shared_account(conn)
        eng.dispose()

        first = tables_meta[0]
        parsed = {
            "tables": tables_meta,
            # 兼容字段：默认指向首个工作表
            "table": first["table"], "schema": first["schema"], "columns": first["columns"],
            "labels": first["labels"], "row_count": total_rows, "col_count": first["col_count"],
        }
        file_store.update(file_id, {"parsed": parsed, "parse_error": None})
        session_registry.touch(session_id)  # S07：记录活跃时间
        logger.info("文件解析完成 file_id=%s tables=%d rows=%d session=%s",
                    file_id, len(tables_meta), total_rows, session_id)
        return parsed

    def drop_table(self, file_id: str, session_id: str) -> None:
        """删除文件时清理临时表与授权，避免数据与权限残留。"""
        entry = file_store.get(file_id)
        parsed = (entry or {}).get("parsed")
        tables = table_names_of(parsed)
        if not tables:
            return
        eng = self._engine()
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for table in tables:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS `{TEMP_DB}`.`{table}`"))
                revoke_session_table(conn, session_id, table)
        eng.dispose()
        logger.info("临时表已清理 tables=%s session=%s", tables, session_id)


ingest_service = FileIngestService()
