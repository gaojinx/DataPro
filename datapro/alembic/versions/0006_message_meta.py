"""message 表启用：补提问人与消息类型，落全量会话消息（含闲聊/澄清）。

Revision ID: 0006_message_meta
Revises: 0005_query_username
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_message_meta"
down_revision = "0005_query_username"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message", sa.Column("username", sa.String(length=64), nullable=True))
    op.add_column("message", sa.Column("kind", sa.String(length=16), nullable=True))
    op.create_index("ix_message_username", "message", ["username"])


def downgrade() -> None:
    op.drop_index("ix_message_username", table_name="message")
    op.drop_column("message", "kind")
    op.drop_column("message", "username")
