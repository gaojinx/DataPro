"""用户令牌版本持久化（管理员/用户权限隔离）。

Revision ID: 0010_token_version
Revises: 0009_db_access
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_token_version"
down_revision = "0009_db_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_account",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("user_account", "token_version")
