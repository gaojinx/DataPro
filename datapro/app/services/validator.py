"""SQL 校验器（HLD §3 / 权限设计 §2.3）：执行前静态检查，全部通过才放行。

检查项：语法 → 写操作 → 危险函数 → 表白名单（含用户级白名单）→ 字段存在性 → 强制 LIMIT → 小基数防泄露。
"""
import sqlglot
from sqlglot import exp
from pydantic import BaseModel, Field


class ValidationResult(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    sql_final: str | None = None  # 补 LIMIT 后的最终 SQL


WRITE_TYPES = {"insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "merge"}


def validate_sql(sql: str, allowed_tables: set[str], user_tables: set[str] | None = None) -> ValidationResult:
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
        if (fname or "").lower() in ("sleep", "benchmark", "load_file", "get_lock", "release_lock"):
            errors.append(f"禁止使用函数: {fname}")

    # 4. 表白名单：AST 提取全部表引用（含子查询），防多层嵌套绕过
    tables = sorted({t.name.lower() for t in parsed.find_all(exp.Table) if t.name})
    bad = [t for t in tables if t not in allowed]
    if bad:
        errors.append(f"引用了白名单之外的表: {', '.join(bad)}")

    # 5. 字段存在性：骨架阶段由调用方注入 column_map（table -> set(col)）；None 则跳过
    # TODO(G3): 接入元数据字典后启用列级校验

    # 6. 强制 LIMIT（默认补 200）
    final_sql = sql.rstrip().rstrip(";")
    if not parsed.args.get("limit"):
        final_sql = f"SELECT * FROM ({final_sql}) _datapro_limit LIMIT 200"

    # 7. 小基数 GROUP BY 防个体泄露（权限设计 §2.3）：骨架规则占位
    # TODO(G3): 执行前估算分组基数，<10 组且含 PII 列时拒绝

    return ValidationResult(passed=not errors, errors=errors, tables=tables, sql_final=final_sql)


class ValidatorService:
    """状态机适配器：Pipeline 统一调用 validate()；用户级表白名单在 G1 权限方案定稿后接入。"""

    async def validate(self, sql: str, allowed_tables: list[str]) -> ValidationResult:
        return validate_sql(sql, {t.lower() for t in allowed_tables})


validator_service = ValidatorService()
