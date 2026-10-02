"""
공통 테스트 픽스처
- test_engine : 테스트 전용 in-memory SQLite (StaticPool → 동일 연결 공유)
- db_session  : 테스트별 독립 세션 (함수 스코프)
- client      : DartService.initialize 모킹 + DB 오버라이드된 TestClient
- auth_headers: 테스트용 API 키 헤더
"""
# 테스트는 항상 in-memory SQLite를 사용한다. .env의 DATABASE_URL(운영 PostgreSQL)을
# 그대로 쓰면 로컬 PG 미기동 시 pytest가 접속 대기로 무한 hang된다. pydantic-settings는
# .env를 os.environ에 주입하지 않으므로, 앱 모듈 import 전에 여기서 env를 강제한다.
# (명시적으로 DATABASE_URL을 export한 경우엔 그 값을 존중 — setdefault)
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# TestClient lifespan마다 prewarm 스레드가 stdout 캡처와 경합하지 않도록 기본 비활성화한다.
# 명시적으로 환경변수를 export한 테스트 실행은 setdefault로 그대로 존중한다.
os.environ.setdefault("ENABLE_BACKGROUND_PREWARM", "False")

# startup toggle 테스트의 threading.Thread 패치 전에 AnyIO worker 기반 클래스를 확정한다.
import anyio._backends._asyncio  # noqa: F401
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

TEST_API_KEY = "pytest-test-api-key-12345"


@pytest.fixture
def test_engine():
    """테스트마다 새 in-memory SQLite DB를 생성한다 (완전 격리)."""
    from app.db.database import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 모든 세션이 동일 커넥션 공유 → 같은 메모리 DB
    )
    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture
def db_session(test_engine):
    """테스트에서 직접 DB를 조작할 때 사용하는 세션."""
    Session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def client(test_engine):
    """
    통합 테스트용 TestClient.
    - get_db : in-memory DB로 오버라이드
    - DartService.initialize : 외부 DART API 호출 차단
    - auth.settings : 테스트용 API 키 주입
    """
    from app.main import app
    from app.db.database import get_db

    Session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    mock_auth_settings = MagicMock()
    mock_auth_settings.API_SECRET_KEY = TEST_API_KEY

    with patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", mock_auth_settings), \
         patch("app.main.SessionLocal", Session), \
         patch("app.api.v1.finance.SessionLocal", Session), \
         patch("app.db.database.SessionLocal", Session), \
         patch("app.main.alembic_command.upgrade"):  # 테스트에선 Alembic 실행 차단
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    return {"X-API-Key": TEST_API_KEY}


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """테스트 간 Rate Limiter 카운터를 초기화한다. 누적으로 인한 429 오탐 방지."""
    from app.dependencies.rate_limit import limiter
    try:
        limiter._storage.reset()
    except Exception:
        pass
    yield
    try:
        limiter._storage.reset()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_finance_caches():
    from app.api.v1.finance import (
        _SECTOR_CACHE,
        _recommendation_cache,
        _similar_cache,
    )
    _SECTOR_CACHE.clear()
    _recommendation_cache.clear()
    _similar_cache.clear()
    yield
    _SECTOR_CACHE.clear()
    _recommendation_cache.clear()
    _similar_cache.clear()
