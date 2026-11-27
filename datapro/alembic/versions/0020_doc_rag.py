"""0020 文档知识库 RAG：kb_doc（文档登记）+ kb_chunk（切块与向量）。

切块是检索单元，向量存 float32 小端字节而非 JSON 文本：1024 维从 15 KB 降到 4 KB，
检索时不需要反序列化整库。`dim` 单独落列，换 embedding 模型后可识别出未 reindex
的旧块（维度不一致直接跳过，避免算出无语义的相似度）。

Revises: 0019_message_payload
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_doc_rag"
down_revision = "0019_message_payload"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "kb_doc" not in existing:
        op.create_table(
            "kb_doc",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("source", sa.String(512), nullable=False, server_default=""),
            sa.Column("doc_type", sa.String(16), nullable=False, server_default="md"),
            sa.Column("category", sa.String(64), nullable=False, server_default="通用"),
            sa.Column("tags", sa.String(255), nullable=False, server_default=""),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content", sa.Text().with_variant(
                sa.dialects.mysql.MEDIUMTEXT(), "mysql"), nullable=True),
            sa.Column("embed_model", sa.String(64), nullable=False, server_default=""),
            sa.Column("content_sha", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_kb_doc_status", "kb_doc", ["status"])
        op.create_index("ix_kb_doc_content_sha", "kb_doc", ["content_sha"])
    if "kb_chunk" not in existing:
        op.create_table(
            "kb_chunk",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                      primary_key=True, autoincrement=True),
            sa.Column("doc_id", sa.String(32), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("section", sa.String(255), nullable=False, server_default=""),
            sa.Column("text", sa.Text(), nullable=False),
            # 向量可能为空（embedding 不可用时先落文本，事后 reindex 补）
            sa.Column("vector", sa.LargeBinary(), nullable=True),
            sa.Column("dim", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_kb_chunk_doc_id", "kb_chunk", ["doc_id"])


def downgrade() -> None:
    existing = _tables()
    if "kb_chunk" in existing:
        op.drop_index("ix_kb_chunk_doc_id", table_name="kb_chunk")
        op.drop_table("kb_chunk")
    if "kb_doc" in existing:
        op.drop_index("ix_kb_doc_content_sha", table_name="kb_doc")
        op.drop_index("ix_kb_doc_status", table_name="kb_doc")
        op.drop_table("kb_doc")
