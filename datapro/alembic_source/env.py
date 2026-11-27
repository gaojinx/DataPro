"""数据源库 Alembic 迁移环境（独立于业务库 alembic/）。

- 连接串取 settings.source_db_url，不在 ini 里落明文；
- target_metadata 只含 BaseSource，避免业务表被误建到数据源库。
"""
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

BASE_DIR = Path(__file__).resolve().parent.parent          # datapro/
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import settings                    # noqa: E402
from app.db.models_source import BaseSource        # noqa: E402

target_metadata = BaseSource.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.source_db_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(settings.source_db_url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
