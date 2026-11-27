"""meta_table / meta_column 建表（D05 元数据导入）

Revision ID: 0004_meta_tables
Revises: 0003_user_permissions
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_meta_tables"
down_revision = "0003_user_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meta_table",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("db_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("cn_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("granularity", sa.String(255), nullable=True),
        sa.Column("update_freq", sa.String(64), nullable=True),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("dt_level", sa.String(16), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(8), nullable=True),
        sa.Column("keywords", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("table_name", name="uq_meta_table_name"),
    )
    op.create_index("ix_meta_table_table_name", "meta_table", ["table_name"])

    op.create_table(
        "meta_column",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("col_name", sa.String(128), nullable=False),
        sa.Column("cn_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("data_type", sa.String(32), nullable=True),
        sa.Column("enum_meaning", sa.String(255), nullable=True),
        sa.Column("caliber_id", sa.String(16), nullable=True),
        sa.Column("is_pii", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sample_values", sa.String(255), nullable=True),
        sa.Column("common_pitfalls", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("table_name", "col_name", name="uq_meta_column"),
    )
    op.create_index("ix_meta_column_table_name", "meta_column", ["table_name"])

    # 口径依赖表与歧义选择关键词：把 recall 里的口径元数据落到库表
    op.add_column("metric_caliber", sa.Column("source_table", sa.String(128), nullable=True))
    op.add_column("metric_caliber", sa.Column("choice_keywords", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("metric_caliber", "choice_keywords")
    op.drop_column("metric_caliber", "source_table")
    op.drop_index("ix_meta_column_table_name", table_name="meta_column")
    op.drop_table("meta_column")
    op.drop_index("ix_meta_table_table_name", table_name="meta_table")
    op.drop_table("meta_table")
