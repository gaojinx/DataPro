"""业务库结构加固：可观测字段、生命周期索引、约束与业务号序列。

对应评审问题的落地：
- P1 零生命周期管理 → audit_log/query_record 补 created_at 索引，配合 worker 定期清理；
- P1 0 外键 0 CHECK → db_access 关联 user_account（级联清理悬挂授权）+ 枚举列 CHECK；
- P1 死表与冗余索引 → 空表 `session` 删除、`meta_column` 前缀重复索引删除；
- P2 query_record 无可观测字段 → 补 outcome/error_code/spec_json/sql_final；
- P2 口径无历史 → 新增 `metric_caliber_history`；
- P2 导出号 1+max 全表扫且并发撞号 → 新增 `id_sequence` 原子分配。

Revision ID: 0013_db_hardening
Revises: 0012_split_ingest_db
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0013_db_hardening"
down_revision = "0012_split_ingest_db"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()

    # ---------- 1. 取数记录：可观测与可追溯字段 ----------
    op.add_column("query_record", sa.Column(
        "outcome", sa.String(16), nullable=False, server_default="unknown"))
    op.add_column("query_record", sa.Column("error_code", sa.String(16), nullable=True))
    op.add_column("query_record", sa.Column("spec_json", sa.Text(), nullable=True))
    op.add_column("query_record", sa.Column("sql_final", sa.Text(), nullable=True))
    # 历史数据尽力回填：能判定执行结果的分三档，其余保持 unknown 不臆测
    op.execute(
        "UPDATE query_record SET outcome = CASE "
        "WHEN executed = 1 THEN 'result' "
        "WHEN validated = 1 THEN 'escalate' "
        "ELSE 'unknown' END "
        "WHERE outcome = 'unknown'")
    op.execute("UPDATE query_record SET sql_final = generated_sql "
               "WHERE sql_final IS NULL AND generated_sql IS NOT NULL")

    op.create_index("ix_query_record_created_at", "query_record", ["created_at"])
    op.create_index("ix_query_record_outcome", "query_record", ["outcome", "created_at"])

    # ---------- 2. 审计日志：生命周期索引（配合定期清理，避免全表扫） ----------
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_user_created", "audit_log", ["user_id", "created_at"])

    # ---------- 3. 口径历史：口径变更可追溯，且不随口径删除而消失 ----------
    op.create_table(
        "metric_caliber_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("metric_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metric_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("aliases", sa.String(255), nullable=True),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("logic", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("source_table", sa.String(128), nullable=True),
        sa.Column("change_type", sa.String(16), nullable=False, server_default="update"),
        sa.Column("changed_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_caliber_history_metric", "metric_caliber_history",
                    ["metric_id", "changed_at"])

    # ---------- 4. 业务号序列：原子分配，替代 1+max(尾部数字) ----------
    op.create_table(
        "id_sequence",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("next_val", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # 从现有最大号续上，避免升级后与历史申请号撞车。
    # 注意存的是「已用到的号」而不是「下一个号」：分配语句每次都会 +1，
    # 写成 max+1 会让首个新号跳号（EP-0001 → EP-0003）。
    op.execute(
        "INSERT INTO id_sequence (name, next_val) "
        "SELECT 'export_request', COALESCE(MAX(CAST(SUBSTRING_INDEX(id, '-', -1) AS UNSIGNED)), 0) "
        "FROM export_request")

    # ---------- 5. 授权表关联用户：删除用户时级联清理，避免悬挂授权 ----------
    op.execute("UPDATE db_access a JOIN user_account u ON u.username = a.username "
               "SET a.user_id = u.id WHERE a.user_id IS NULL")
    op.create_foreign_key("fk_db_access_username", "db_access", "user_account",
                          ["username"], ["username"], ondelete="CASCADE")
    # 外键已由 uq_db_access(username, ...) 覆盖前缀，单列索引冗余
    op.drop_index("ix_db_access_username", table_name="db_access")

    # meta_column 的 (db_name, table_name) 索引是唯一键 (db_name, table_name, col_name) 的前缀
    op.drop_index("ix_meta_column_table_name", table_name="meta_column")

    # ---------- 6. 枚举列 CHECK：把「应用层约定」变成库层约束 ----------
    op.create_check_constraint("ck_message_role", "message", "role IN ('user', 'assistant')")
    op.create_check_constraint("ck_user_role", "user_account", "role IN ('admin', 'user')")
    op.create_check_constraint("ck_user_status", "user_account",
                               "status IN ('active', 'disabled')")
    op.create_check_constraint("ck_query_feedback", "query_record",
                               "user_feedback IS NULL OR user_feedback IN ('up', 'down')")
    op.create_check_constraint("ck_export_status", "export_request",
                               "status IN ('pending', 'approved', 'rejected', 'failed')")
    op.create_check_constraint("ck_metric_status", "metric_caliber",
                               "status IN ('active', 'conflict')")

    # ---------- 7. 死表：session 无写入且无读取，空表才删 ----------
    session_rows = bind.execute(sa.text("SELECT COUNT(*) FROM `session`")).scalar_one()
    if session_rows == 0:
        op.drop_table("session")
        logger.info("已删除空表 session（会话真值在 Redis + sessions.json）")
    else:
        logger.warning("session 表有 %s 行数据，跳过删除（请先确认数据来源）", session_rows)


def downgrade() -> None:
    op.create_table(
        "session",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])

    for name, table in (
        ("ck_metric_status", "metric_caliber"),
        ("ck_export_status", "export_request"),
        ("ck_query_feedback", "query_record"),
        ("ck_user_status", "user_account"),
        ("ck_user_role", "user_account"),
        ("ck_message_role", "message"),
    ):
        op.drop_constraint(name, table, type_="check")

    op.create_index("ix_meta_column_table_name", "meta_column", ["db_name", "table_name"])
    op.create_index("ix_db_access_username", "db_access", ["username"])
    op.drop_constraint("fk_db_access_username", "db_access", type_="foreignkey")

    op.drop_table("id_sequence")
    op.drop_index("ix_caliber_history_metric", table_name="metric_caliber_history")
    op.drop_table("metric_caliber_history")

    op.drop_index("ix_audit_log_user_created", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")

    op.drop_index("ix_query_record_outcome", table_name="query_record")
    op.drop_index("ix_query_record_created_at", table_name="query_record")
    op.drop_column("query_record", "sql_final")
    op.drop_column("query_record", "spec_json")
    op.drop_column("query_record", "error_code")
    op.drop_column("query_record", "outcome")
