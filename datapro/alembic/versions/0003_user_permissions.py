"""user_account 增加权限覆盖列（B12 权限矩阵）

Revision ID: 0003_user_permissions
Revises: 0002_langfuse_trace
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_user_permissions"
down_revision = "0002_langfuse_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # JSON 数组文本；NULL 表示继承角色默认权限
    op.add_column("user_account", sa.Column("permissions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_account", "permissions")
