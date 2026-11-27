"""0019 消息终态载荷：message.payload 存终态 SSE 事件 JSON（result/clarify/reject…）。

跨设备恢复消息流时，助手消息不再只有纯文本——结果表格/KPI/依据/澄清卡片
都可以从 payload 还原；步骤时间轴是瞬态不存。MEDIUMTEXT 上限 16MB，
覆盖 200 行 × 宽表的结果载荷。

Revises: 0018_message_attachments
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_message_payload"
down_revision = "0018_message_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "payload" not in cols:
        op.add_column("message", sa.Column(
            "payload", sa.Text().with_variant(sa.dialects.mysql.MEDIUMTEXT(), "mysql"),
            nullable=True))


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "payload" in cols:
        op.drop_column("message", "payload")
