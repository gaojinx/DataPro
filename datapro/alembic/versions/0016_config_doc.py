"""配置文档存储（0016）：把 data/*.json 配置域收进业务库。

收口剩余配置域（技能/知识库/定时任务/巡检规则与结果/口径裁决/学习数据/记忆/
文件索引/会话登记）的统一做法，而不是逐域改服务：
`SharedJson` 在 `CONFIG_STORE=db` 时把同一份文档写进 `config_doc`，
读写接口保持 `snapshot()/edit()` 不变，服务层零改动。

为什么用「文档表」而不是逐域关系化：
- 这些域的共同诉求是「多实例强一致 + 不丢数据 + 可审计」，不是复杂条件查询；
- 逐域建表要改 9 套服务与缓存，收益主要是可 JOIN，当前无此需求；
- 需要按字段查询时再单独拆某一个域（如 llm_api/role_permission 已拆）。

Revision ID: 0016_config_doc
Revises: 0015_access_request
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0016_config_doc"
down_revision = "0015_access_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_doc",
        # 文档键 = 相对 data/ 的路径，如 skills.json、uploads/index.json
        sa.Column("doc_key", sa.String(191), primary_key=True),
        sa.Column("payload", sa.Text().with_variant(LONGTEXT(), "mysql"),
                  nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_config_doc_updated", "config_doc", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_config_doc_updated", table_name="config_doc")
    op.drop_table("config_doc")
