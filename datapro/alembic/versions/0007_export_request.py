"""导出申请表（B13/F11）：记录审批状态、生成文件与导出结果。

Revision ID: 0007_export_request
Revises: 0006_message_meta
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_export_request"
down_revision = "0006_message_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_request",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("requester", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("preview_rows", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.String(length=255), nullable=True),
        sa.Column("file", sa.String(length=128), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("exported_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_export_request_requester", "export_request", ["requester"])
    op.create_index("ix_export_request_status", "export_request", ["status"])


def downgrade() -> None:
    op.drop_index("ix_export_request_status", table_name="export_request")
    op.drop_index("ix_export_request_requester", table_name="export_request")
    op.drop_table("export_request")
