from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

from app.core.config import settings
from app.db.database import Base

# app/db/models.py 임포트 — autogenerate가 모델을 인식하려면 반드시 필요
import app.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# 우리 모델의 메타데이터 — autogenerate 기반
target_metadata = Base.metadata

# .env의 DATABASE_URL을 alembic.ini 대신 사용
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """오프라인 모드: DB 연결 없이 SQL 스크립트만 생성"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """온라인 모드: 실제 DB에 연결하여 마이그레이션 실행"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,       # 컬럼 타입 변경도 감지
            render_as_batch=is_sqlite,  # SQLite는 ALTER TABLE 미지원 → batch 모드
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
