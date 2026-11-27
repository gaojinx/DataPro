"""配置域关系化（第一批）：LLM API 注册表与角色权限矩阵。

为什么先做这两个域：
- `llm_apis.json` 存明文 Key，且管理员改模型/换 Key 无库级留痕；
- `role_permissions.json` 是授权依据，多实例下需要「写一次全实例立刻生效」的强一致读，
  文件锁能保证不写坏，但读侧缓存与跨机共享仍受限。

其余配置域（技能/知识库/定时任务/巡检/裁决/学习记忆/文件索引）按同样模式分批迁移，
不在本迁移内一次动完：每域都要改服务、缓存与测试，混在一次迁移里出问题无法回滚。

Revision ID: 0014_config_domains
Revises: 0013_db_hardening
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_config_domains"
down_revision = "0013_db_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- LLM API 注册表 ----------
    op.create_table(
        "llm_api",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        # 明文存储与文件时代一致，但受库权限与备份策略保护；生产应换 KMS/Vault 引用
        sa.Column("api_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_api_default", "llm_api", ["is_default", "enabled"])

    # ---------- 角色权限矩阵 ----------
    op.create_table(
        "role_permission",
        sa.Column("role", sa.String(16), primary_key=True),
        sa.Column("permissions", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("role_permission")
    op.drop_index("ix_llm_api_default", table_name="llm_api")
    op.drop_table("llm_api")
