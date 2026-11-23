"""召回服务：向量 + 关键词双路召回 → 精排（HLD §3）。

骨架阶段：关键词内存召回 + 降级模式；接入 Milvus 后启用向量路。
召回不到 → 空 tables → 状态机直接 REJECT(E002)，绝不给模型自由发挥空间。
"""
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings


@dataclass
class RecallResult:
    tables: list[dict] = field(default_factory=list)   # [{table_name, schema, description, score}]
    calibers: list[dict] = field(default_factory=list) # [{metric_id, metric_name, definition, logic}]


# 骨架种子元数据：由元数据字典（DataPro-元数据与口径库模板）导入替换
# keywords 为人工维护的检索关键词（接入向量库后由语义召回替代）
SEED_TABLES = [
    {"table_name": "dwd_order_detail_di", "schema": "dwd_shop",
     "description": "订单明细日报表，一行=一笔订单一个商品，含支付状态、退款标记",
     "keywords": ["订单", "销量", "销售额", "支付", "退款", "GMV", "品类", "渠道"],
     "columns": ["order_id", "user_id", "pay_status", "amount", "category", "channel", "dt"]},
    {"table_name": "dwd_user_di", "schema": "dwd_shop",
     "description": "用户行为日报表，含注册、登录、活跃标记",
     "keywords": ["用户", "注册", "登录", "活跃", "日活"],
     "columns": ["user_id", "register_date", "login_date", "is_active", "dt"]},
    {"table_name": "dim_channel", "schema": "dim",
     "description": "渠道维表", "keywords": ["渠道"], "columns": ["channel_id", "channel_name"]},
]

SEED_CALIBERS = [
    {"metric_id": "M003", "metric_name": "支付订单数", "aliases": ["付费订单量", "成交订单数"],
     "table": "dwd_order_detail_di",
     "definition": "当日支付成功且未退款的订单去重数",
     "logic": "COUNT(DISTINCT order_id) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M001", "metric_name": "日活跃用户数", "aliases": ["日活", "DAU"],
     "table": "dwd_user_di",
     "definition": "当日有登录行为的用户去重数",
     "logic": "COUNT(DISTINCT user_id) WHERE dt='{date}'"},
]

_IGNORED_NAME_PARTS = {"dwd", "dim", "di", "df", "ods", "dws"}


def _table_match(table: dict, q: str) -> bool:
    """表名或核心片段命中：支持"order_detail表"这类用户口语引用。"""
    name = table["table_name"].lower()
    if name in q:
        return True
    return any(p in q for p in name.split("_")
               if len(p) >= 4 and p not in _IGNORED_NAME_PARTS)


def _caliber_match(caliber: dict, q: str) -> bool:
    """口径匹配：全名、别名，以及去掉"数/率/量"后缀的词根（如"支付订单"）。"""
    for name in [caliber["metric_name"]] + caliber.get("aliases", []):
        if name in q:
            return True
        stem = name.rstrip("数率量")
        if len(stem) >= 3 and stem in q:
            return True
    return False


class RecallService:
    async def recall(self, question: str, session_id: str | None = None) -> RecallResult:
        if settings.recall_degraded_mode != "force_keyword":
            pass  # TODO(G1): 向量路（Milvus）+ 关键词路合并 → Rerank 精排
        q = question.lower()
        tables = [t for t in SEED_TABLES
                  if _table_match(t, q) or any(k.lower() in q for k in t.get("keywords", []))]
        calibers = [c for c in SEED_CALIBERS if _caliber_match(c, q)]
        # 会话内已解析的上传文件表（F27）
        from app.services.file_store import file_store
        tables = self._file_tables(q, session_id, file_store) + tables
        return RecallResult(tables=tables, calibers=calibers)

    @staticmethod
    def _file_tables(q: str, session_id: str | None, store) -> list[dict]:
        if not session_id:
            return []
        parsed = store.parsed_files(session_id)
        if not parsed:
            return []
        hits = []
        for f in parsed:
            p = f["parsed"]
            stem = Path(f["name"]).stem.lower()
            matched = bool(stem) and stem in q
            # 会话内仅一个已解析文件时，"这个文件/表里"等指代默认命中
            if not matched and len(parsed) == 1 and any(
                    k in q for k in ("文件", "表格", "上传", "附件", "这份", "这个表", "表里")):
                matched = True
            if not matched:
                continue
            tables_meta = p.get("tables") or [p]
            multi = len(tables_meta) > 1
            for tm in tables_meta:
                desc = (f"用户上传文件「{f['name']}」"
                        + (f" · 工作表「{tm.get('sheet') or 'Sheet1'}」" if multi else "")
                        + f"（{tm['row_count']} 行 × {tm['col_count']} 列）")
                hits.append({
                    "table_name": tm["table"],
                    "schema": tm.get("schema") or p.get("schema"),
                    "description": desc,
                    "keywords": [],
                    "columns": tm["columns"],
                    "column_labels": tm["labels"],
                })
        return hits


recall_service = RecallService()
