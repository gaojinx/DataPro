"""0017 密钥引用化：llm_api.api_key（明文）→ api_key_ref（env:/file:/kms:// 引用）。

为什么不能直接 DROP：库里已有的明文 Key 是真实凭据，直接删列等于删数据。
本迁移把明文搬到 data/secrets/llm_apis.env（0600、已被 gitignore），行内改为
file: 引用；管理员后续应改用 env:/kms: 引用（见日志提示）。写文件失败则中止，
绝不出现「列删了、Key 丢了」的状态。

Revises: 0016_config_doc
Create Date: 2026-11-26
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0017_secret_ref"
down_revision = "0016_config_doc"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.0017")


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("llm_api")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "api_key_ref" not in cols:
        op.add_column("llm_api",
                      sa.Column("api_key_ref", sa.String(255), nullable=False,
                                server_default="", comment="密钥引用 env:/file:/kms://"))

    # 搬运明文：列还在才搬（重复升级/已迁移过的库直接跳过）
    if "api_key" in cols:
        from app.services.secret_ref import extract_plaintext

        rows = bind.execute(sa.text("SELECT id, api_key FROM llm_api")).fetchall()
        migrated = 0
        for row in rows:
            key = (row.api_key or "").strip()
            if not key:
                continue
            var = f"LLM_KEY_{str(row.id).upper()}"
            ref = extract_plaintext(key, var)
            bind.execute(sa.text("UPDATE llm_api SET api_key_ref = :r WHERE id = :i"),
                         {"r": ref, "i": row.id})
            migrated += 1
        if migrated:
            logger.warning(
                "已把 %d 个明文 LLM Key 搬到 data/secrets/llm_apis.env 并改为 file: 引用；"
                "生产请尽快改用 env:/kms: 引用并删除该文件", migrated)
        op.drop_column("llm_api", "api_key")


def downgrade() -> None:
    """恢复明文列：env:/file: 引用尽力回填；kms: 引用无法离线解析，置空并告警。"""
    bind = op.get_bind()
    if "api_key" not in _columns(bind):
        op.add_column("llm_api",
                      sa.Column("api_key", sa.String(255), nullable=False,
                                server_default="", comment="已废弃：明文 Key（回滚用）"))
    rows = bind.execute(sa.text("SELECT id, api_key_ref FROM llm_api")).fetchall()
    from app.services.secret_ref import SecretRefError, resolve

    for row in rows:
        ref = (row.api_key_ref or "").strip()
        if not ref:
            continue
        try:
            bind.execute(sa.text("UPDATE llm_api SET api_key = :k WHERE id = :i"),
                         {"k": resolve(ref), "i": row.id})
        except SecretRefError as exc:
            logger.warning("回滚时无法解析 %s 的引用 %s：%s", row.id, ref, exc)
