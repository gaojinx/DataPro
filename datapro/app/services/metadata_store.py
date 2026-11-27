"""元数据存储（D05）：业务库优先，库空或不可用时回退内置种子。

- 读路径：`meta_table` + `meta_column` + `metric_caliber` 组装成召回所需结构，60 秒缓存
- 写路径：`importer.import_metadata` 从填报模板导入后调用 `invalidate()` 立即生效
- 降级：业务库不可用（未迁移/连不上）只告警一次，继续用内置种子，不影响取数
"""
import logging
import threading
import time

logger = logging.getLogger("datapro.metadata")

TTL_SECONDS = 60.0


def merge_tables(db_rows: list[dict], seed_rows: list[dict]) -> list[dict]:
    """库表覆盖同名的种子表，其余种子保留（便于分批导入）。"""
    merged = {t["table_name"]: dict(t) for t in seed_rows}
    for row in db_rows:
        merged[row["table_name"]] = row
    return list(merged.values())


def merge_calibers(db_rows: list[dict], seed_rows: list[dict]) -> list[dict]:
    """按 metric_id 覆盖；库表缺的字段（如 choice_keywords）沿用种子值。"""
    merged = {c["metric_id"]: dict(c) for c in seed_rows}
    for row in db_rows:
        base = merged.get(row["metric_id"], {})
        base.update({k: v for k, v in row.items() if v not in (None, "")})
        merged[row["metric_id"]] = base
    return list(merged.values())


class MetadataStore:
    def __init__(self, ttl: float = TTL_SECONDS):
        self._ttl = ttl
        self._lock = threading.RLock()
        self._tables: list[dict] | None = None
        self._calibers: list[dict] | None = None
        self._loaded_at = 0.0
        self._warned = False

    def tables(self) -> list[dict]:
        return self._get()[0]

    def calibers(self) -> list[dict]:
        return self._get()[1]

    def invalidate(self) -> None:
        with self._lock:
            self._tables = self._calibers = None
            self._loaded_at = 0.0

    # ---------- 内部 ----------
    def _get(self) -> tuple[list[dict], list[dict]]:
        with self._lock:
            fresh = self._tables is not None and time.monotonic() - self._loaded_at < self._ttl
            if fresh:
                return self._tables, self._calibers
            # 延迟导入：recall 反向依赖本模块，模块加载期不能互相 import
            from app.services.recall import SEED_CALIBERS, SEED_TABLES

            db_tables: list[dict] = []
            db_calibers: list[dict] = []
            try:
                db_tables, db_calibers = self._read_db()
            except Exception as exc:  # noqa: BLE001 —— 库不可用时降级种子，不阻塞主链路
                if not self._warned:
                    logger.warning("读取元数据库失败，回退内置种子：%s", exc)
                    self._warned = True
            self._tables = merge_tables(db_tables, SEED_TABLES)
            self._calibers = merge_calibers(db_calibers, SEED_CALIBERS)
            self._loaded_at = time.monotonic()
            return self._tables, self._calibers

    @staticmethod
    def _read_db() -> tuple[list[dict], list[dict]]:
        from app.db.models import MetaColumn, MetaTable, MetricCaliber
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            cols: dict[str, list] = {}
            for c in db.query(MetaColumn).all():
                cols.setdefault(c.table_name, []).append(c)
            tables = []
            for t in db.query(MetaTable).filter(MetaTable.status == "active").all():
                col_rows = cols.get(t.table_name, [])
                keywords = [k.strip() for k in (t.keywords or "").replace("，", ",").split(",") if k.strip()]
                if not keywords:
                    keywords = [t.cn_name] if t.cn_name else []
                keywords += [c.cn_name for c in col_rows if c.cn_name]
                tables.append({
                    "table_name": t.table_name,
                    "cn_name": t.cn_name or "",
                    "schema": t.db_name,
                    "description": t.description or t.cn_name or t.table_name,
                    "keywords": list(dict.fromkeys(keywords)),
                    "columns": [c.col_name for c in col_rows],
                    "column_labels": [c.cn_name for c in col_rows if c.cn_name],
                    # 字段级元数据（与 columns 逐位对齐）：字段级检索的二级文档、prompt 的
                    # 中文名与业务提示都从这里取；缺失时调用方回退到 columns/column_labels
                    "column_meta": [{
                        "name": c.col_name,
                        "cn_name": c.cn_name or "",
                        "type": c.data_type or "",
                        "enum": c.enum_meaning or "",
                        "pitfall": c.common_pitfalls or "",
                        "pii": bool(c.is_pii),
                    } for c in col_rows],
                    # 枚举值：让生成模型看到合法取值（如 region=华东，而不是“东北区域”）
                    "column_enums": {c.col_name: c.enum_meaning
                                     for c in col_rows if c.enum_meaning},
                })
            calibers = [{
                "metric_id": c.metric_id,
                "metric_name": c.metric_name,
                "aliases": [a.strip() for a in (c.aliases or "").replace("，", ",").split(",") if a.strip()],
                "table": c.source_table,
                "definition": c.definition,
                "logic": c.logic,
                "choice_keywords": [k.strip() for k in (c.choice_keywords or "").split(",") if k.strip()],
                "status": c.status,
            } for c in db.query(MetricCaliber).all()]
            return tables, calibers


metadata_store = MetadataStore()
