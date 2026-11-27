"""数据表权限自助申请（U-B05）：越权 E003 后不必线下找管理员。

流程：用户提交 → 管理员审批 → 通过后写入 `db_access`（带有效期）→ 校验器下一次请求即可放行。
约束（来自后端缺口分析 U-B05）：
- 不改状态机：拦截仍在校验阶段按 A02 表级 E003 生效，申请只是“补授权”的旁路；
- 授权必须带有效期，且审批留痕（reviewed_by/reviewed_at/review_note）；
- 撤销与删用户清权沿用 `db_access`（username 外键 ON DELETE CASCADE）。

Revision ID: 0015_access_request
Revises: 0014_config_domains
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_access_request"
down_revision = "0014_config_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_request",
        sa.Column("id", sa.String(16), primary_key=True),          # AR-0001（id_sequence 分配）
        sa.Column("requester", sa.String(64), nullable=False),
        sa.Column("db_name", sa.String(64), nullable=False, server_default="dwd_shop"),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False, server_default=""),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),     # 仅通过后有值
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')",
                           name="ck_access_request_status"),
    )
    op.create_index("ix_access_request_requester", "access_request",
                    ["requester", "created_at"])
    op.create_index("ix_access_request_status", "access_request", ["status", "created_at"])

    # 授权有效期：NULL = 永久（手工授予沿用旧语义，存量行不需要回填）
    op.add_column("db_access", sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.create_index("ix_db_access_expires", "db_access", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_db_access_expires", table_name="db_access")
    op.drop_column("db_access", "expires_at")
    op.drop_index("ix_access_request_status", table_name="access_request")
    op.drop_index("ix_access_request_requester", table_name="access_request")
    op.drop_table("access_request")
