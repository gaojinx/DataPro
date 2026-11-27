"""0018 消息附件引用：message.attachments 存附件指针快照（JSON 数组）。

只存 {file_id,name,ext,size,origin} 引用，不存文件内容；历史消息恢复后文件
仍可点击预览，文件访问继续走 files 接口原有的 owner/admin 鉴权。

Revises: 0017_secret_ref
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_message_attachments"
down_revision = "0017_secret_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "attachments" not in cols:
        # MySQL TEXT 不能带默认值，NULL 表示无附件
        op.add_column("message", sa.Column("attachments", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "attachments" in cols:
        op.drop_column("message", "attachments")
