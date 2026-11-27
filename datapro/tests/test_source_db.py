"""数据源库拆分（0012）：配置推导、模型隔离、迁移树独立。"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import Settings

DATAPRO_DIR = Path(__file__).resolve().parents[1]


def test_source_url_derived_from_app_db():
    s = Settings(app_database_url="mysql+pymysql://u:p@127.0.0.1:3306/datapro",
                 source_database_url="")
    assert s.source_db_url == "mysql+pymysql://u:p@127.0.0.1:3306/datapro_source"


def test_source_url_explicit_wins():
    s = Settings(app_database_url="mysql+pymysql://u:p@127.0.0.1:3306/datapro",
                 source_database_url="mysql+pymysql://svc:q@db:3306/src_prod")
    assert s.source_db_url == "mysql+pymysql://svc:q@db:3306/src_prod"


def test_source_url_requires_db_name():
    s = Settings(app_database_url="mysql+pymysql://u:p@127.0.0.1:3306/",
                 source_database_url="")
    try:
        s.source_db_url
    except ValueError as exc:
        assert "SOURCE_DATABASE_URL" in str(exc)
    else:
        raise AssertionError("缺少库名时应报错，而不是生成非法连接串")


def test_ingest_models_moved_out_of_business_metadata():
    from app.db.models import Base
    from app.db.models_source import BaseSource

    business = set(Base.metadata.tables)
    source = set(BaseSource.metadata.tables)
    ingest = {"ingest_source", "ingest_dataset", "ingest_field_map",
              "ingest_batch", "ingest_row_error"}

    assert not (business & ingest), "数据源表不能留在业务库 Base 里"
    assert ingest <= source
    # 业务表也不能被数据源库基线顺手建出来
    assert "query_record" not in source and "user_account" not in source


def _head(script_location: str, ini: str) -> tuple[list[str], dict]:
    cfg = Config(str(DATAPRO_DIR / ini))
    cfg.set_main_option("script_location", str(DATAPRO_DIR / script_location))
    script = ScriptDirectory.from_config(cfg)
    heads = list(script.get_heads())
    revisions = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    return heads, revisions


def test_migration_trees_are_independent():
    biz_heads, biz_revs = _head("alembic", "alembic.ini")
    src_heads, src_revs = _head("alembic_source", "alembic_source.ini")

    assert biz_heads == ["0021_meta_embedding"]
    assert src_heads == ["0001_ingest_layer"]
    assert biz_revs["0012_split_ingest_db"] == "0011_ingest_layer"
    assert src_revs["0001_ingest_layer"] is None
    # 两棵树不共享任何 revision id
    assert not (set(biz_revs) & set(src_revs))
