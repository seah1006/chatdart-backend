"""
우선순위 높음 기능 테스트
- Refresh Token Rotation (2건)
- 비밀번호 강도 검증 (4건)
- 관리자 통계 API (4건)
- 이메일 미인증 로그인 차단 옵션 (3건)
"""
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

TEST_VALID_PASSWORD = "Abcd1234!@"


@pytest.fixture
def hp_engine():
    from app.db.database import Base
    import app.db.models  # noqa: F401
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture
def hp_client(hp_engine):
    from app.main import app
    from app.db.database import get_db

    Session = sessionmaker(autocommit=False, autoflush=False, bind=hp_engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with patch("app.services.dart_service.DartService.initialize"), \
         patch("app.main.alembic_command.upgrade"):
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


@pytest.fixture
def hp_db(hp_engine):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=hp_engine)
    db = Session()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def reset_limiter():
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


def _register(client, email="user@example.com", password=TEST_VALID_PASSWORD):
    return client.post("/api/v1/auth/register", json={
        "email": email, "password": password, "nickname": "테스터", "terms_agreed": True,
    })


def _login(client, email="user@example.com", password=TEST_VALID_PASSWORD):
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json().get("access_token"), resp.json().get("refresh_token")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── Refresh Token Rotation ────────────────────────────────────────────────────

def test_refresh_returns_new_refresh_token(hp_client):
    """refresh 호출 시 새 refresh_token이 함께 반환된다."""
    _register(hp_client)
    _, old_refresh = _login(hp_client)

    resp = hp_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert "refresh_token" in body
    assert body["refresh_token"] != old_refresh


def test_old_refresh_token_invalid_after_rotation(hp_client):
    """rotation 후 이전 refresh_token은 401을 반환한다."""
    _register(hp_client)
    _, old_refresh = _login(hp_client)

    hp_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})

    resp = hp_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 401


# ── 비밀번호 강도 검증 ────────────────────────────────────────────────────────

def test_register_password_no_digit_returns_422(hp_client):
    """숫자가 없는 비밀번호로 회원가입 시 422가 반환된다."""
    resp = hp_client.post("/api/v1/auth/register", json={
        "email": "a@example.com", "password": "passwordabc!", "terms_agreed": True,
    })
    assert resp.status_code == 422


def test_register_password_no_letter_returns_422(hp_client):
    """영문자가 없는 비밀번호로 회원가입 시 422가 반환된다."""
    resp = hp_client.post("/api/v1/auth/register", json={
        "email": "a@example.com", "password": "1234567890!", "terms_agreed": True,
    })
    assert resp.status_code == 422


def test_register_strong_password_succeeds(hp_client):
    """숫자+영문+특수문자 혼합 비밀번호로 회원가입이 성공한다."""
    resp = _register(hp_client, password=TEST_VALID_PASSWORD)
    assert resp.status_code == 201


def test_update_password_no_digit_returns_422(hp_client):
    """PATCH /me에서 숫자 없는 새 비밀번호는 422가 반환된다."""
    _register(hp_client)
    access, _ = _login(hp_client)
    resp = hp_client.patch("/api/v1/users/me", json={
        "current_password": TEST_VALID_PASSWORD,
        "new_password": "newpasswordabc",
    }, headers=_auth(access))
    assert resp.status_code == 422


# ── 관리자 통계 API ───────────────────────────────────────────────────────────

def test_admin_stats_no_auth_returns_401(hp_client):
    """인증 없이 /admin/stats 요청 시 401이 반환된다."""
    resp = hp_client.get("/api/v1/admin/stats")
    assert resp.status_code == 401


def test_admin_stats_non_admin_returns_403(hp_client):
    """일반 유저로 /admin/stats 요청 시 403이 반환된다."""
    _register(hp_client)
    access, _ = _login(hp_client)
    resp = hp_client.get("/api/v1/admin/stats", headers=_auth(access))
    assert resp.status_code == 403


def test_admin_stats_returns_structure(hp_client, hp_db):
    """관리자로 /admin/stats 요청 시 필수 필드가 모두 반환된다."""
    from app.db.models import User
    from app.core.security import hash_password

    admin = User(
        email="admin@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_admin=True,
        is_active=True,
        is_verified=True,
    )
    hp_db.add(admin)
    hp_db.commit()

    resp = hp_client.post("/api/v1/auth/login", json={
        "email": "admin@example.com", "password": TEST_VALID_PASSWORD,
    })
    access = resp.json()["access_token"]

    resp = hp_client.get("/api/v1/admin/stats", headers=_auth(access))
    assert resp.status_code == 200
    body = resp.json()
    for field in ("total_users", "active_users", "verified_users",
                  "collected_companies", "unanswered_inquiries",
                  "today_searches", "searches_30d"):
        assert field in body, f"'{field}' 필드 누락"


def test_admin_stats_counts_correctly(hp_client, hp_db):
    """가입 유저 수와 활성 유저 수가 정확히 반환된다."""
    from app.db.models import User
    from app.core.security import hash_password

    admin = User(
        email="admin@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_admin=True, is_active=True, is_verified=True,
    )
    inactive = User(
        email="inactive@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_active=False, is_verified=False,
    )
    hp_db.add_all([admin, inactive])
    hp_db.commit()

    resp = hp_client.post("/api/v1/auth/login", json={
        "email": "admin@example.com", "password": TEST_VALID_PASSWORD,
    })
    access = resp.json()["access_token"]

    body = hp_client.get("/api/v1/admin/stats", headers=_auth(access)).json()
    assert body["total_users"] == 2
    assert body["active_users"] == 1


# ── 이메일 미인증 로그인 차단 옵션 ───────────────────────────────────────────────

def test_unverified_login_allowed_by_default(hp_client, hp_db):
    """REQUIRE_EMAIL_VERIFICATION=False(기본)이면 미인증 계정도 로그인 가능."""
    from app.db.models import User
    from app.core.security import hash_password

    hp_db.add(User(
        email="unverified@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_active=True, is_verified=False,
    ))
    hp_db.commit()

    resp = hp_client.post("/api/v1/auth/login", json={
        "email": "unverified@example.com", "password": TEST_VALID_PASSWORD,
    })
    assert resp.status_code == 200


def test_unverified_login_blocked_when_required(hp_client, hp_db):
    """REQUIRE_EMAIL_VERIFICATION=True이면 미인증 계정 로그인 시 403."""
    from app.db.models import User
    from app.core.security import hash_password
    from app.core.config import settings

    hp_db.add(User(
        email="unverified@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_active=True, is_verified=False,
    ))
    hp_db.commit()

    with patch.object(settings, "REQUIRE_EMAIL_VERIFICATION", True):
        resp = hp_client.post("/api/v1/auth/login", json={
            "email": "unverified@example.com", "password": TEST_VALID_PASSWORD,
        })
    assert resp.status_code == 403


def test_verified_login_succeeds_when_required(hp_client, hp_db):
    """REQUIRE_EMAIL_VERIFICATION=True여도 인증된 계정은 로그인 가능."""
    from app.db.models import User
    from app.core.security import hash_password
    from app.core.config import settings

    hp_db.add(User(
        email="verified@example.com",
        hashed_password=hash_password(TEST_VALID_PASSWORD),
        is_active=True, is_verified=True,
    ))
    hp_db.commit()

    with patch.object(settings, "REQUIRE_EMAIL_VERIFICATION", True):
        resp = hp_client.post("/api/v1/auth/login", json={
            "email": "verified@example.com", "password": TEST_VALID_PASSWORD,
        })
    assert resp.status_code == 200
