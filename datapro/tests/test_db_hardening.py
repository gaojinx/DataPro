"""业务库结构加固（0013）：可观测字段、口径历史、业务号序列、迁移链。"""
from pathlib import Path
from types import SimpleNamespace

from alembic.config import Config
from alembic.script import ScriptDirectory

DATAPRO_DIR = Path(__file__).resolve().parents[1]


def test_migration_chain_reaches_0016():
    cfg = Config(str(DATAPRO_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(DATAPRO_DIR / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert list(script.get_heads()) == ["0021_meta_embedding"]
    revs = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    assert revs["0013_db_hardening"] == "0012_split_ingest_db"
    assert revs["0014_config_domains"] == "0013_db_hardening"
    assert revs["0015_access_request"] == "0014_config_domains"
    assert revs["0016_config_doc"] == "0015_access_request"
    assert revs["0017_secret_ref"] == "0016_config_doc"
    assert revs["0018_message_attachments"] == "0017_secret_ref"
    assert revs["0019_message_payload"] == "0018_message_attachments"
    assert revs["0020_doc_rag"] == "0019_message_payload"
    assert revs["0021_meta_embedding"] == "0020_doc_rag"
    assert revs["0021_meta_embedding"] == "0020_doc_rag"


def test_query_record_has_observability_columns():
    from app.db.models import QueryRecord

    cols = set(QueryRecord.__table__.columns.keys())
    assert {"outcome", "error_code", "spec_json", "sql_final"} <= cols


def test_new_tables_registered_and_dead_table_removed():
    from app.db import models

    tables = set(models.Base.metadata.tables)
    assert "metric_caliber_history" in tables
    assert "id_sequence" in tables
    assert "session" not in tables          # 0013 已删除空表
    assert not hasattr(models, "Session")   # 模型同步移除，避免 autogenerate 重建


def test_db_access_has_user_id_and_unique_key():
    from app.db.models import DbAccess

    assert "user_id" in DbAccess.__table__.columns
    uniques = {c.name for c in DbAccess.__table__.constraints if c.name}
    assert "uq_db_access" in uniques


def test_caliber_history_snapshot_copies_fields():
    from app.services.caliber_history import snapshot

    added = []
    db = SimpleNamespace(add=added.append)
    caliber = SimpleNamespace(metric_id="M001", version=3, metric_name="支付订单数",
                              aliases="支付单量", definition="已支付订单数",
                              logic="COUNT(DISTINCT order_id)", owner="data",
                              status="active", source_table="dwd_order_detail_di")

    snapshot(db, caliber, "update", "admin")

    assert len(added) == 1
    row = added[0]
    assert row.metric_id == "M001"
    assert row.version == 3
    assert row.logic == "COUNT(DISTINCT order_id)"
    assert row.change_type == "update"
    assert row.changed_by == "admin"


def test_pipeline_context_carries_outcome_and_sql_final():
    from app.core.statemachine import PipelineContext

    ctx = PipelineContext(question="q", user_id="u")
    assert ctx.outcome == ""
    assert ctx.error_code == ""
    assert ctx.sql_final is None


def test_merge_records_uses_explicit_total():
    """两表只取了当前页行数，总数必须由 COUNT 传入，不能被 len(merged) 截断。"""
    from app.services.persist import merge_records

    items, total = merge_records([{"id": 1, "created_at": "2026-01-01 00:00:00"}], [],
                                 page=1, page_size=10, total=57)
    assert len(items) == 1
    assert total == 57
