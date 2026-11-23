"""数据库连接与初始化。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(
    settings.app_database_url,
    pool_pre_ping=True,
    pool_size=5,
    pool_recycle=1800,
    connect_args={"connect_timeout": 3},   # 业务库不可用时快速失败，不拖慢主链路
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app.db.models import Base
    # 生产环境用 Alembic 迁移；骨架阶段直接建表
    Base.metadata.create_all(engine)
