"""返回层脱敏（S04）：查询结果返回前对 PII 打码。

原则：
- 只改展示值，不改数值列（否则会算错数、也可能把聚合结果弄脏）
- 两道路径：列名命中 PII 关键词 → 整列打码；单元格内容命中格式规则 → 局部打码
- 规则可配置：默认规则内置，`MASK_EXTRA_COLUMNS` 追加列名关键词，`MASK_ENABLED` 一键关闭
- 打码在结果事件生成处统一执行，前端表格/CSV 导出/审计落库拿到的都是脱敏后的行
"""
import re

from app.config import settings

# 列名命中即整列打码：只认明确的 PII 列，不能用宽泛的 name（会把品类名/渠道名误伤）
COLUMN_PATTERNS = (
    re.compile(r"(^|_)(phone|mobile|tel|contact)(_|$)"),
    re.compile(r"(^|_)(id_card|id_no|id_number|identity)(_|$)"),
    re.compile(r"(^|_)(bank_card|card_no|bank_account)(_|$)"),
    re.compile(r"(^|_)(email|mail)(_|$)"),
    re.compile(r"(^|_)(address|addr)(_|$)"),
    re.compile(r"^(user_name|real_name|full_name|nickname|customer_name|member_name)$"),
)


def _phone(m: re.Match) -> str:
    v = m.group(0)
    return f"{v[:3]}****{v[-4:]}"


def _id_card(m: re.Match) -> str:
    v = m.group(0)
    return f"{v[:6]}********{v[-4:]}"


def _bank_card(m: re.Match) -> str:
    v = m.group(0)
    return f"{'*' * (len(v) - 4)}{v[-4:]}"


def _email(m: re.Match) -> str:
    return f"{m.group(1)}***@{m.group(2)}"


# 顺序敏感：身份证（18 位）要先于银行卡（16–19 位）
VALUE_RULES = (
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _phone),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), _id_card),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), _bank_card),
    ("email", re.compile(r"([\w.+-])[\w.+-]*@([\w-]+(?:\.[\w-]+)+)"), _email),
)


def _column_hit(column: str, extra: tuple[str, ...]) -> bool:
    name = column.lower()
    if any(k and k in name for k in extra):
        return True
    return any(p.search(name) for p in COLUMN_PATTERNS)


# 个体标识列：按这些列 GROUP BY 等于返回个人明细（A07）。channel_id 等维表外键不在此列
IDENTIFIER_PATTERN = re.compile(
    r"^(user_id|uid|member_id|customer_id|buyer_id|order_id|order_no|trade_no|session_id|device_id)$")


def is_pii_column(column: str) -> bool:
    """列名是否属于 PII（手机号/证件/银行卡/邮箱/地址/姓名）。"""
    return any(p.search(column.lower()) for p in COLUMN_PATTERNS)


def is_identifier_column(column: str) -> bool:
    """列名是否为个体/单据级标识，不能作为分组维度。"""
    return bool(IDENTIFIER_PATTERN.match(column.lower()))


def mask_value(value: str) -> tuple[str, dict[str, int]]:
    """按内容规则打码，返回（新值, 各规则命中次数）。"""
    hits: dict[str, int] = {}
    out = value
    for name, pattern, repl in VALUE_RULES:
        out, n = pattern.subn(repl, out)
        if n:
            hits[name] = hits.get(name, 0) + n
    return out, hits


def mask_rows(rows: list[dict], columns: list[str] | None = None,
              extra_keywords: tuple[str, ...] | None = None,
              enabled: bool | None = None) -> tuple[list[dict], dict[str, int]]:
    """对结果行脱敏；返回（脱敏后的行, 各规则命中次数）。

    命中列名规则的整列替换为固定掩码（保留首尾字符，长度足够时）。
    """
    if enabled is None:
        enabled = settings.mask_enabled
    if not enabled or not rows:
        return rows, {}

    extra = extra_keywords
    if extra is None:
        extra = tuple(k.strip().lower() for k in settings.mask_extra_columns.split(",") if k.strip())
    cols = columns or sorted({k for row in rows for k in row})

    full_mask_cols = {c for c in cols if _column_hit(str(c), extra)}
    stats: dict[str, int] = {}
    masked: list[dict] = []
    for row in rows:
        new_row = dict(row)
        for col, val in row.items():
            if not isinstance(val, str) or not val:
                continue
            if col in full_mask_cols:
                v = val
                new_row[col] = f"{v[0]}{'*' * max(4, len(v) - 2)}{v[-1]}" if len(v) > 2 else "*" * len(v)
                stats["column"] = stats.get("column", 0) + 1
                continue
            new_val, hits = mask_value(val)
            if hits:
                new_row[col] = new_val
                for name, n in hits.items():
                    stats[name] = stats.get(name, 0) + n
        masked.append(new_row)
    return masked, stats
