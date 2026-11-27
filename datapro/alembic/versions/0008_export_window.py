"""导出申请补时间窗（B13）：审批重跑 SQL 时必须沿用原查询的时间范围。

Revision ID: 0008_export_window
Revises: 0007_export_request
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_export_window"
down_revision = "0007_export_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("export_request", sa.Column("window_start", sa.Date(), nullable=True))
    op.add_column("export_request", sa.Column("window_end", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("export_request", "window_end")
    op.drop_column("export_request", "window_start")
