"""先知投放报表的字段映射、清洗、汇总行排除与幂等键。

一手证据：归档 xlsx（1 表头 + 82 数据行 × 15 列）已复核；
唯一键必须含 landing_page_name，否则 plan_id='0' 的两行会被误合并。
本模块只处理数据，不接触凭据、不接触数据库，便于独立单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# 中文表头 → ASCII 列名（严格按架构 §3.6，顺序与源报表一致）
HEADER_MAP: dict[str, str] = {
    "投放日期": "dt",
    "投放账户名称": "account_name",
    "媒体": "media",
    "投放计划名称": "plan_name",
    "落地页名称": "landing_page_name",
    "投放计划ID": "plan_id",
    "展示": "impressions",
    "点击": "clicks",
    "账面消耗": "account_cost",
    "登录用户数": "login_users",
    "引流课报名人数": "lead_signups",
    "引流课报名成本": "lead_signup_cost",
    "高一引流课报名人数": "grade1_signups",
    "高二引流课报名人数": "grade2_signups",
    "高三引流课报名人数": "grade3_signups",
}

# 写库列顺序（系统列由 writer 追加）
DATA_COLUMNS: tuple[str, ...] = tuple(HEADER_MAP.values())

TEXT_COLUMNS: frozenset[str] = frozenset(
    {"account_name", "media", "plan_name", "landing_page_name", "plan_id"})
INT_COLUMNS: frozenset[str] = frozenset(
    {"impressions", "clicks", "login_users", "lead_signups",
     "grade1_signups", "grade2_signups", "grade3_signups"})
DECIMAL_COLUMNS: frozenset[str] = frozenset({"account_cost", "lead_signup_cost"})
NUMERIC_COLUMNS: frozenset[str] = INT_COLUMNS | DECIMAL_COLUMNS

# 源端空值标记；plan_id='0' 是合法值，绝不能当 NULL
_NULL_TOKENS: frozenset[str] = frozenset({"", "-", "--", "—", "null", "none", "nan", "n/a", "na"})
_SUMMARY_TOKENS: tuple[str, ...] = ("汇总", "合计", "总计", "小计")

# ingest_field_map 登记用的规范定义（CSV 模板与登记脚本都以此为准）
# (src_field, col_name, data_type, transform, required, comment)
FIELD_SPECS: tuple[tuple[str, str, str, str, bool, str], ...] = (
    ("投放日期", "dt", "DATE", "date_parse", True, "源端日期，YYYY-MM-DD"),
    ("投放账户名称", "account_name", "VARCHAR(128)", "dash_to_null", False, "空值归一为空串"),
    ("媒体", "media", "VARCHAR(64)", "dash_to_null", True, "保留原文，不做枚举归一"),
    ("投放计划名称", "plan_name", "VARCHAR(255)", "dash_to_null", False, "空值归一为空串"),
    ("落地页名称", "landing_page_name", "VARCHAR(255)", "dash_to_null", True, "幂等键一部分；-- 归一为空串"),
    ("投放计划ID", "plan_id", "VARCHAR(64)", "trim_number", True, "按字符串保留，0 是合法值"),
    ("展示", "impressions", "BIGINT UNSIGNED", "trim_number", False, "空值为 NULL，0 为源端明确为 0"),
    ("点击", "clicks", "BIGINT UNSIGNED", "trim_number", False, "空值为 NULL"),
    ("账面消耗", "account_cost", "DECIMAL(18,4)", "trim_number", False, "与 dwd_campaign_daily.cost 口径不同"),
    ("登录用户数", "login_users", "INT UNSIGNED", "trim_number", False, "空值为 NULL"),
    ("引流课报名人数", "lead_signups", "INT UNSIGNED", "trim_number", False, "高一/二/三是拆分，不可与总数相加"),
    ("引流课报名成本", "lead_signup_cost", "DECIMAL(18,4)", "trim_number", False, "源端给定值，勿用消耗/报名自行相除"),
    ("高一引流课报名人数", "grade1_signups", "INT UNSIGNED", "trim_number", False, "年级拆分维度"),
    ("高二引流课报名人数", "grade2_signups", "INT UNSIGNED", "trim_number", False, "年级拆分维度"),
    ("高三引流课报名人数", "grade3_signups", "INT UNSIGNED", "trim_number", False, "年级拆分维度"),
)


@dataclass
class RawRow:
    """驱动层产出的原始行：cells 以中文表头为键，row_no 为源表行号（含表头偏移）。"""

    cells: dict[str, Any] = field(default_factory=dict)
    row_no: int = 0


class FieldMapper:
    """把 RawRow 归一成可写库的 ASCII 行，并给出坏行样本。"""

    HEADER_MAP = HEADER_MAP

    @staticmethod
    def _is_null_token(value: Any) -> bool:
        if value is None:
            return True
        return isinstance(value, str) and value.strip().lower() in _NULL_TOKENS

    @staticmethod
    def clean_cell(value: Any, col: str) -> Any:
        """按列类型清洗单个单元格：文本空值→''，数值空值→None，0 保持为 0。"""
        if FieldMapper._is_null_token(value):
            return "" if col in TEXT_COLUMNS else None
        if col == "dt":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            text = str(value).strip()
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            return None
        if col == "plan_id":
            # 可能是 0、纯数字字符串或带小数的 Excel 读出值；一律当字符串。
            text = str(value).strip()
            if text.endswith(".0") and text[:-2].isdigit():
                return text[:-2]
            return text
        if col in TEXT_COLUMNS:
            return str(value).strip()
        if col in INT_COLUMNS | DECIMAL_COLUMNS:
            text = str(value).strip().replace(",", "").replace(" ", "")
            text = text.replace("，", "")
            if text.lower() in _NULL_TOKENS:
                return None
            try:
                number = Decimal(text)
            except (InvalidOperation, ValueError):
                return None
            if not number.is_finite():
                return None
            if col in INT_COLUMNS:
                return int(number)
            return number.quantize(Decimal("0.0001"))
        return value

    @staticmethod
    def is_summary_row(row: dict[str, Any]) -> bool:
        """命中任一即排除：文本含汇总词 / dt 非日期 / 数值与文本同时为空。"""
        text_cols = TEXT_COLUMNS - {"plan_id"}
        for col in text_cols:
            value = str(row.get(col) or "")
            if any(token in value for token in _SUMMARY_TOKENS):
                return True
        if not isinstance(row.get("dt"), date):
            return True
        if all(row.get(c) in (None, "") for c in NUMERIC_COLUMNS) and all(
                not str(row.get(c) or "") for c in text_cols):
            return True
        return False

    @staticmethod
    def idempotency_key(row: dict[str, Any]) -> str:
        """四列业务键；用不可见分隔符避免内容里含普通分隔符造成碰撞。"""
        parts = (
            row.get("dt").isoformat() if isinstance(row.get("dt"), date) else str(row.get("dt") or ""),
            str(row.get("plan_id") or ""),
            str(row.get("media") or ""),
            str(row.get("landing_page_name") or ""),
        )
        return "\x1f".join(parts)

    def normalize(self, rows: list[RawRow]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回（有效行, 坏行样本）。坏行样本只含源行文本，不含凭据。"""
        valid: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for raw in rows:
            mapped: dict[str, Any] = {}
            for cn_header, value in raw.cells.items():
                col = HEADER_MAP.get(str(cn_header).strip())
                if col:
                    mapped[col] = self.clean_cell(value, col)
            # 未出现的列补默认值，保证下游字段齐全
            for col in DATA_COLUMNS:
                mapped.setdefault(col, "" if col in TEXT_COLUMNS else None)
            if self.is_summary_row(mapped):
                errors.append({"row_no": raw.row_no, "reason": "summary_row",
                               "raw_row": self._safe_raw(raw.cells)})
                continue
            mapped["_idempotency_key"] = self.idempotency_key(mapped)
            valid.append(mapped)
        return valid, errors

    @staticmethod
    def _safe_raw(cells: dict[str, Any], limit: int = 1000) -> str:
        """坏行原文截断；只保留源报表单元格，永不写凭据引用解析结果。"""
        text = str({str(k): str(v) for k, v in cells.items()})
        return text[:limit]
