"""query_record 增加 Langfuse trace 关联列（F48 可观测）

Revision ID: 0002_langfuse_trace
Revises: 0001_baseline
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_langfuse_trace"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("query_record", sa.Column("langfuse_trace_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("query_record", "langfuse_trace_id")
