"""SQL 校验器（HLD §3 / 权限设计 §2.3）：执行前静态检查，全部通过才放行。

检查项：语法 → 写操作 → 危险函数 → 表白名单（含用户级白名单）→ 字段存在性 → 强制 LIMIT → 小基数防泄露。
"""
import re

import sqlglot
from sqlglot import exp
from pydantic import BaseModel, Field

from app.services import masking


class ValidationResult(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    sql_final: str | None = None  # 补 LIMIT 后的最终 SQL
    forbidden: list[str] = Field(default_factory=list)  # 用户无权访问的表（A02，直接 E003）


WRITE_TYPES = {"insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "merge"}

# 非 MySQL 方言函数：执行期会报 1305/1370 等错误，提前拦截并回炉修正
UNSUPPORTED_FUNCS = {"date_trunc", "date_part", "generate_series", "to_char", "to_date",
                     "age", "justify_days", "make_date", "split_part"}


def validate_sql(sql: str, allowed_tables: set[str], user_tables: set[str] | None = None,
                 column_map: dict[str, set[str]] | None = None) -> ValidationResult:
    errors: list[str] = []
    allowed = allowed_tables if user_tables is None else allowed_tables & user_tables

    # 1. 语法解析
    try:
        parsed = sqlglot.parse_one(sql, read="mysql")
    except sqlglot.errors.ParseError as e:
        return ValidationResult(passed=False, errors=[f"语法错误: {e}"])

    # 2. 写操作检测（AST 根节点类型）
    if parsed.key in WRITE_TYPES:
        return ValidationResult(passed=False, errors=["禁止任何写操作，仅支持 SELECT"])

    if not isinstance(parsed, exp.Select):
        errors.append("仅支持 SELECT 查询")

    # 3. 危险函数（sqlglot 将未知函数解析为 Anonymous，需同时取真实函数名）
    for fn in parsed.find_all(exp.Func):
        fname = fn.sql_name()
        if fname == "ANONYMOUS":
            fname = getattr(fn, "name", "")
        low = (fname or "").lower()
        if low in ("sleep", "benchmark", "load_file", "get_lock", "release_lock"):
            errors.append(f"禁止使用函数: {fname}")
        elif low in UNSUPPORTED_FUNCS:
            errors.append(f"不支持 MySQL 方言函数: {fname}（请改用 MySQL 函数或 {{date_start}}/{{date_end}} 占位符）")

    # 3.1 PostgreSQL 风格间隔字面量（INTERVAL '7' DAY）在 MySQL 下不可执行
    if re.search(r"INTERVAL\s+'", sql, flags=re.IGNORECASE):
        errors.append("不支持 INTERVAL '7' DAY 写法，请使用 MySQL 的 INTERVAL 7 DAY 或日期占位符")

    # 4. 表白名单：AST 提取全部表引用（含子查询），防多层嵌套绕过
    tables = sorted({t.name.lower() for t in parsed.find_all(exp.Table) if t.name})
    bad = [t for t in tables if t not in allowed]
    if bad:
        errors.append(f"引用了白名单之外的表: {', '.join(bad)}")

    # 4.1 用户级表白名单（A02/S1）：有配置时，越权表单独标记，由状态机直接 E003 拒答，
    #     不走重试（重试也无法获得权限，只会浪费模型调用）
    forbidden: list[str] = []
    if user_tables is not None:
        owned = {t.lower() for t in user_tables}
        forbidden = [t for t in tables if t not in owned]
        if forbidden:
            errors.append(f"无权访问数据表: {', '.join(forbidden)}（数据权限 A02）")

    # 5. 字段存在性：column_map 由调用方按召回表注入（table -> 列名集合），未注入则跳过
    if column_map:
        aliases = {(a.alias_or_name or "").lower() for a in parsed.find_all(exp.Alias)}
        known = set().union(*column_map.values()) if column_map else set()
        unknown = set()
        for col in parsed.find_all(exp.Column):
            name = (col.name or "").lower()
            if not name or name == "*" or name in aliases or name in known:
                continue
            unknown.add(name)
        if unknown:
            sample = "、".join(sorted(known)[:15])
            errors.append(f"引用了不存在的字段: {', '.join(sorted(unknown))}；可用字段: {sample}")

    # 6. 强制 LIMIT（默认补 200）
    final_sql = sql.rstrip().rstrip(";")
    if not parsed.args.get("limit"):
        final_sql = f"SELECT * FROM ({final_sql}) _datapro_limit LIMIT 200"

    # 7. 小基数 GROUP BY 防个体泄露（权限设计 §2.3 / A07）
    # 执行前无法预知分组基数，因此拦截「按个体/敏感列分组」这种必然泄露的写法：
    # 按 user_id/order_id 分组 = 返回个人明细；按手机号/邮箱/证件分组同理。
    # channel_id、category 等维表外键/业务维度正常放行。
    group = parsed.args.get("group")
    group_cols: list[str] = []
    for g in (group.expressions if group else []):
        if isinstance(g, exp.Column):
            group_cols.append((g.name or "").lower())
    leaked = [c for c in group_cols
              if masking.is_pii_column(c) or masking.is_identifier_column(c)]
    if leaked:
        errors.append(f"按个体/敏感列分组会暴露明细（A07 防泄露）: {', '.join(sorted(set(leaked)))}；"
                      f"请改为按天/渠道/品类等业务维度分组")

    return ValidationResult(passed=not errors, errors=errors, tables=tables,
                            sql_final=final_sql, forbidden=forbidden)


class ValidatorService:
    """状态机适配器：Pipeline 统一调用 validate()，按用户级表白名单收紧（A02）。"""

    async def validate(self, sql: str, allowed_tables: list[str],
                       column_map: dict[str, set[str]] | None = None,
                       user_tables: set[str] | None = None) -> ValidationResult:
        return validate_sql(sql, {t.lower() for t in allowed_tables},
                            user_tables=user_tables, column_map=column_map)


validator_service = ValidatorService()
