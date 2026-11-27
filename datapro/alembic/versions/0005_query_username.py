"""query_record 增加提问人（管理后台按用户筛选对话记录）。

Revision ID: 0005_query_username
Revises: 0004_meta_tables
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_query_username"
down_revision = "0004_meta_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("query_record", sa.Column("username", sa.String(length=64), nullable=True))
    op.create_index("ix_query_record_username", "query_record", ["username"])


def downgrade() -> None:
    op.drop_index("ix_query_record_username", table_name="query_record")
    op.drop_column("query_record", "username")
