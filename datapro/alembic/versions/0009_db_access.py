"""用户级数据表权限表（A02 / S1）。

Revision ID: 0009_db_access
Revises: 0008_export_window
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_db_access"
down_revision = "0008_export_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "db_access",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("db_name", sa.String(length=64), nullable=False,
                  server_default="dwd_shop"),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("granted_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", "db_name", "table_name", name="uq_db_access"),
    )
    op.create_index("ix_db_access_username", "db_access", ["username"])


def downgrade() -> None:
    op.drop_index("ix_db_access_username", table_name="db_access")
    op.drop_table("db_access")
