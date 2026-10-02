from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

# SQLite는 파일 기반 단일 연결이므로 풀 설정 불필요.
# PostgreSQL은 환경별로 풀 크기를 조정한다.
_pool_kwargs: dict = {}
if not _is_sqlite:
    if settings.is_production:
        _pool_kwargs = {"pool_size": 20, "max_overflow": 30}
    else:
        _pool_kwargs = {"pool_size": 10, "max_overflow": 10}
    _pool_kwargs.update({"pool_timeout": 30, "pool_recycle": 1800})

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,  # PostgreSQL: 끊어진 연결 자동 감지·교체
    **_pool_kwargs,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        # PRAGMA busy_timeout 제거 — lock 대기는 connect_args timeout=15(초)로 단일화.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
