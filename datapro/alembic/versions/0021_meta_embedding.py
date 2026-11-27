"""0021 元数据向量缓存：meta_embedding。

表/口径/字段共约 130 份文档，走 API embedding 全量编码要 2~5 秒。没有缓存时，
每个进程启动、每次元数据文本变化都要重算一遍——「重启」直接变成「首问必慢」，
多副本还会乘以副本数。落库后只编码真正变了的文档。

`uid + text_sha + model` 三者共同决定缓存是否命中，所以换模型不需要手工清理：
旧行自然失效，由 warmup 逐步覆盖。

Revises: 0020_doc_rag
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_meta_embedding"
down_revision = "0020_doc_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "meta_embedding" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "meta_embedding",
        sa.Column("uid", sa.String(191), primary_key=True),
        sa.Column("doc_type", sa.String(16), nullable=False, server_default=""),
        sa.Column("text_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(64), nullable=False, server_default=""),
        sa.Column("dim", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vector", sa.LargeBinary(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_meta_embedding_doc_type", "meta_embedding", ["doc_type"])


def downgrade() -> None:
    if "meta_embedding" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_index("ix_meta_embedding_doc_type", table_name="meta_embedding")
        op.drop_table("meta_embedding")
