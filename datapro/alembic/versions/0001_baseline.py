"""基线：用户/会话/消息/取数记录/审计/口径六张表

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-24

说明：该版本对应引入 Alembic 之前的 create_all 结果（含 question_context 列）。
已存在的库在应用启动时由 init_db() 自动 stamp 到本版本，不会重复建表。
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_account_username", "user_account", ["username"], unique=True)

    op.create_table(
        "session",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"], unique=False)

    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_session_id", "message", ["session_id"], unique=False)

    op.create_table(
        "query_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_context", sa.Text(), nullable=True),
        sa.Column("recalled_schema", sa.Text(), nullable=True),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("validated", sa.Boolean(), nullable=False),
        sa.Column("executed", sa.Boolean(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("user_feedback", sa.String(length=8), nullable=True),
        sa.Column("feedback_reason", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_record_session_id", "query_record", ["session_id"], unique=False)
    op.create_index("ix_query_record_trace_id", "query_record", ["trace_id"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=16), nullable=False),
        sa.Column("question_digest", sa.String(length=255), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_trace_id", "audit_log", ["trace_id"], unique=False)

    op.create_table(
        "metric_caliber",
        sa.Column("metric_id", sa.String(length=16), nullable=False),
        sa.Column("metric_name", sa.String(length=64), nullable=False),
        sa.Column("aliases", sa.String(length=255), nullable=True),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("logic", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("metric_id"),
    )
    op.create_index("ix_metric_caliber_metric_name", "metric_caliber", ["metric_name"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_metric_caliber_metric_name", table_name="metric_caliber")
    op.drop_table("metric_caliber")
    op.drop_index("ix_audit_log_trace_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_query_record_trace_id", table_name="query_record")
    op.drop_index("ix_query_record_session_id", table_name="query_record")
    op.drop_table("query_record")
    op.drop_index("ix_message_session_id", table_name="message")
    op.drop_table("message")
    op.drop_index("ix_session_user_id", table_name="session")
    op.drop_table("session")
    op.drop_index("ix_user_account_username", table_name="user_account")
    op.drop_table("user_account")
