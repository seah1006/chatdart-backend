"""
Phase 1 보안 패치 통합 테스트

1-1. 결제 검증 강제화 — production 환경에서 PG 키 없으면 503
1-2. 로그아웃 후 access token 즉시 무효화 (tokens_valid_after)
1-3. 이메일 발송 실패 처리 — 응답에 email_sent 필드 포함
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

TEST_VALID_PASSWORD = "Abcd1234!@"
TEST_VALID_PASSWORD_2 = "Abcd1234#$"


# ── 공통 픽스처 ───────────────────────────────────────────────────────────────

@pytest.fixture
def p1_engine():
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
def p1_client(p1_engine):
    from app.main import app
    from app.db.database import get_db

    Session = sessionmaker(autocommit=False, autoflush=False, bind=p1_engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.main.alembic_command.upgrade"):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


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
    data = resp.json()
    return data.get("access_token"), data.get("refresh_token")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── 1-1. 결제 검증 강제화 ─────────────────────────────────────────────────────

# NOTE: 이 클래스는 asyncio.run()으로 async 함수를 직접 실행한다.
# 표준 pytest 동기 모드에서만 안전하다.
# pytest-asyncio를 asyncio_mode="auto"로 도입할 경우
# asyncio.run() → @pytest.mark.asyncio + await 로 전환 필요.
class TestPgVerification:
    def test_production_settings_requires_portone_payment_secrets(self):
        """운영 환경 Settings 생성 시 PortOne 결제 시크릿 누락을 한 번에 보고한다."""
        from app.core.config import Settings
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            Settings(
                ENV="production",
                DART_API_KEY="dart",
                API_SECRET_KEY="api-secret",
                JWT_SECRET_KEY="jwt-secret",
                SENTRY_DSN="https://example@sentry.io/1",
                WEBHOOK_SECRET="",
                PORTONE_API_KEY="",
                PORTONE_API_SECRET="",
                PORTONE_WEBHOOK_SECRET="",
            )

        message = str(exc_info.value)
        assert "PORTONE_API_KEY" in message
        assert "PORTONE_API_SECRET" in message
        assert "PORTONE_WEBHOOK_SECRET" in message

    def test_development_settings_allows_missing_payment_secrets(self):
        """개발 환경은 기존처럼 PG 키와 웹훅 시크릿 없이 Settings 생성이 가능하다."""
        from app.core.config import Settings

        settings = Settings(
            ENV="development",
            DART_API_KEY="dart",
            API_SECRET_KEY="api-secret",
            JWT_SECRET_KEY="jwt-secret",
            WEBHOOK_SECRET="",
            PORTONE_API_KEY="",
            PORTONE_API_SECRET="",
            PORTONE_WEBHOOK_SECRET="",
        )

        assert settings.WEBHOOK_SECRET == ""

    def test_production_portone_no_key_raises_503(self):
        """운영 환경에서 포트원 API 키 미설정 시 503 반환."""
        from app.services.pg_service import _verify_portone
        import asyncio

        mock_prod_settings = MagicMock()
        mock_prod_settings.is_production = True
        mock_prod_settings.PORTONE_API_KEY = ""
        mock_prod_settings.PORTONE_API_SECRET = ""

        with patch("app.services.pg_service.settings", mock_prod_settings):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(_verify_portone("imp_test_123", 9900))
            assert exc_info.value.status_code == 503

    def test_development_portone_no_key_returns_true(self):
        """개발 환경에서 포트원 키 미설정 시 검증 스킵(True 반환)."""
        from app.services.pg_service import _verify_portone
        import asyncio

        mock_dev_settings = MagicMock()
        mock_dev_settings.is_production = False
        mock_dev_settings.PORTONE_API_KEY = ""
        mock_dev_settings.PORTONE_API_SECRET = ""

        with patch("app.services.pg_service.settings", mock_dev_settings):
            result = asyncio.run(_verify_portone("imp_test_123", 9900))
        assert result is True

    def test_production_toss_no_key_raises_503(self):
        """운영 환경에서 토스 시크릿 키 미설정 시 503 반환."""
        from app.services.pg_service import _verify_toss
        import asyncio

        mock_prod_settings = MagicMock()
        mock_prod_settings.is_production = True
        mock_prod_settings.TOSS_SECRET_KEY = ""

        with patch("app.services.pg_service.settings", mock_prod_settings):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(_verify_toss("toss_key_abc", 9900))
            assert exc_info.value.status_code == 503

    def test_development_toss_no_key_returns_true(self):
        """개발 환경에서 토스 키 미설정 시 검증 스킵(True 반환)."""
        from app.services.pg_service import _verify_toss
        import asyncio

        mock_dev_settings = MagicMock()
        mock_dev_settings.is_production = False
        mock_dev_settings.TOSS_SECRET_KEY = ""

        with patch("app.services.pg_service.settings", mock_dev_settings):
            result = asyncio.run(_verify_toss("toss_key_abc", 9900))
        assert result is True

    def test_production_unknown_provider_raises_503(self):
        """운영 환경에서 PG_PROVIDER 미설정/비지원 시 503 반환."""
        from app.services.pg_service import verify_payment
        import asyncio

        mock_prod_settings = MagicMock()
        mock_prod_settings.is_production = True
        mock_prod_settings.PG_PROVIDER = "unknown_pg"

        with patch("app.services.pg_service.settings", mock_prod_settings):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(verify_payment("tx_123", 9900))
            assert exc_info.value.status_code == 503

    def test_development_unknown_provider_returns_true(self):
        """개발 환경에서 PG_PROVIDER 미설정/비지원 시 스킵(True 반환)."""
        from app.services.pg_service import verify_payment
        import asyncio

        mock_dev_settings = MagicMock()
        mock_dev_settings.is_production = False
        mock_dev_settings.PG_PROVIDER = "unknown_pg"

        with patch("app.services.pg_service.settings", mock_dev_settings):
            result = asyncio.run(verify_payment("tx_123", 9900))
        assert result is True

    def test_webhook_fails_when_pg_verification_fails(self, p1_client, p1_engine):
        """PG 검증 실패 시 웹훅 엔드포인트가 400 반환하고 PaymentRecord가 failed로 변경."""
        from app.db.models import PaymentRecord
        from sqlalchemy.orm import sessionmaker

        _register(p1_client)
        access_token, _ = _login(p1_client)

        # 구독 → payment_id 발급
        resp = p1_client.post("/api/v1/membership/subscribe",
                              json={"plan": "pro"},
                              headers=_auth(access_token))
        assert resp.status_code == 200
        payment_id = resp.json()["payment_id"]

        # PG 검증 실패 시뮬레이션
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=False):
            webhook_resp = p1_client.post("/api/v1/membership/webhook", json={
                "payment_id": payment_id,
                "pg_transaction_id": "imp_fake_001",
                "status": "paid",
                "amount": 9900,
            }, headers={"X-Webhook-Secret": "test-secret"})
        assert webhook_resp.status_code == 400

        # DB에서 PaymentRecord 상태 확인
        Session = sessionmaker(bind=p1_engine)
        db = Session()
        record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == payment_id).first()
        db.close()
        assert record.status == "failed"

    def test_webhook_succeeds_with_valid_pg_verification(self, p1_client, p1_engine):
        """PG 검증 성공 시 멤버십이 활성화됨."""
        from app.db.models import UserMembership
        from sqlalchemy.orm import sessionmaker

        _register(p1_client)
        access_token, _ = _login(p1_client)

        resp = p1_client.post("/api/v1/membership/subscribe",
                              json={"plan": "pro"},
                              headers=_auth(access_token))
        payment_id = resp.json()["payment_id"]

        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            webhook_resp = p1_client.post("/api/v1/membership/webhook", json={
                "payment_id": payment_id,
                "pg_transaction_id": "imp_real_001",
                "status": "paid",
                "amount": 9900,
            }, headers={"X-Webhook-Secret": "test-secret"})
        assert webhook_resp.status_code == 200

        Session = sessionmaker(bind=p1_engine)
        db = Session()
        from app.db.models import User
        user = db.query(User).filter(User.email == "user@example.com").first()
        membership = db.query(UserMembership).filter(
            UserMembership.user_id == user.id,
            UserMembership.status == "active",
        ).first()
        db.close()
        assert membership is not None
        assert membership.plan == "pro"


# ── 1-2. 로그아웃 후 access token 즉시 무효화 ────────────────────────────────

class TestTokenRevocation:
    def test_access_token_invalid_after_logout(self, p1_client):
        """로그아웃 후 기존 access_token으로 보호된 엔드포인트 접근 시 401."""
        _register(p1_client)
        access_token, refresh_token = _login(p1_client)

        # 로그아웃 전 정상 접근
        resp_before = p1_client.get("/api/v1/users/me", headers=_auth(access_token))
        assert resp_before.status_code == 200

        # 로그아웃
        logout_resp = p1_client.post("/api/v1/auth/logout",
                                     json={"refresh_token": refresh_token})
        assert logout_resp.status_code == 204

        # 로그아웃 후 동일 access_token 사용 → 401
        resp_after = p1_client.get("/api/v1/users/me", headers=_auth(access_token))
        assert resp_after.status_code == 401
        assert "무효화" in resp_after.json()["detail"]

    def test_refresh_token_invalid_after_logout(self, p1_client):
        """로그아웃 후 refresh_token으로 토큰 갱신 시도 시 401."""
        _register(p1_client)
        _, refresh_token = _login(p1_client)

        p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

        resp = p1_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 401

    def test_new_token_valid_after_relogin(self, p1_client):
        """로그아웃 후 재로그인으로 발급된 토큰은 정상 동작."""
        _register(p1_client)
        _, refresh_token = _login(p1_client)

        p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

        # 재로그인
        new_access, new_refresh = _login(p1_client)
        assert new_access is not None

        resp = p1_client.get("/api/v1/users/me", headers=_auth(new_access))
        assert resp.status_code == 200

    def test_multiple_logout_is_idempotent(self, p1_client):
        """이미 만료된 토큰으로 로그아웃해도 204 반환 (멱등성)."""
        _register(p1_client)
        _, refresh_token = _login(p1_client)

        p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
        resp = p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
        assert resp.status_code == 204

    def test_tokens_valid_after_set_on_logout(self, p1_client, p1_engine):
        """로그아웃 시 DB에서 tokens_valid_after가 설정됨."""
        from app.db.models import User
        from sqlalchemy.orm import sessionmaker

        _register(p1_client)
        _, refresh_token = _login(p1_client)

        Session = sessionmaker(bind=p1_engine)
        db = Session()
        user_before = db.query(User).filter(User.email == "user@example.com").first()
        assert user_before.tokens_valid_after is None
        db.close()

        p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

        db = Session()
        user_after = db.query(User).filter(User.email == "user@example.com").first()
        assert user_after.tokens_valid_after is not None
        db.close()

    def test_delete_account_invalidates_access_token(self, p1_client):
        """회원 탈퇴 후 기존 access_token 사용 시 401."""
        _register(p1_client)
        access_token, _ = _login(p1_client)

        # 탈퇴
        p1_client.delete("/api/v1/users/me", headers=_auth(access_token))

        # 탈퇴 후 동일 토큰으로 접근 시도
        resp = p1_client.get("/api/v1/users/me", headers=_auth(access_token))
        assert resp.status_code == 401

    def test_change_password_invalidates_existing_sessions(self, p1_client):
        """비밀번호 변경 후 기존 access_token으로 접근 시 401."""
        _register(p1_client)
        access_token, _ = _login(p1_client)

        # 비밀번호 변경
        resp = p1_client.patch("/api/v1/users/me", headers=_auth(access_token), json={
            "current_password": TEST_VALID_PASSWORD,
            "new_password": TEST_VALID_PASSWORD_2,
        })
        assert resp.status_code == 200

        # 변경 전 토큰으로 접근 시도 → 401
        resp = p1_client.get("/api/v1/users/me", headers=_auth(access_token))
        assert resp.status_code == 401
        assert "무효화" in resp.json()["detail"]

    def test_require_auth_rejects_revoked_jwt(self, p1_client):
        """require_auth 경로(finance 엔드포인트)에서도 로그아웃 후 JWT 무효화 적용."""
        _register(p1_client)
        access_token, refresh_token = _login(p1_client)

        # 로그아웃
        p1_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

        # finance 엔드포인트를 JWT Bearer로 접근 → 401
        resp = p1_client.get(
            "/api/v1/finance/search",
            params={"keyword": "삼성"},
            headers=_auth(access_token),
        )
        assert resp.status_code == 401
        assert "무효화" in resp.json()["detail"]

    def test_revoked_user_rejects_access_token_without_iat(self, p1_client, p1_engine):
        """tokens_valid_after 설정 후 iat 없는 access_token은 거부한다."""
        from datetime import datetime, timedelta, timezone
        import jwt
        from sqlalchemy.orm import sessionmaker

        from app.core.config import settings
        from app.db.models import User

        _register(p1_client)
        _login(p1_client)

        Session = sessionmaker(bind=p1_engine)
        db = Session()
        user = db.query(User).filter(User.email == "user@example.com").first()
        user.tokens_valid_after = datetime.now(timezone.utc)
        db.commit()
        user_id = user.id
        email = user.email
        db.close()

        token_without_iat = jwt.encode(
            {
                "sub": str(user_id),
                "email": email,
                "type": "access",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        resp = p1_client.get("/api/v1/users/me", headers=_auth(token_without_iat))
        assert resp.status_code == 401
        assert "무효화" in resp.json()["detail"]


# ── 1-3. 이메일 발송 실패 처리 ───────────────────────────────────────────────

class TestEmailSentField:
    def test_register_without_mail_config_returns_email_sent_false(self, p1_client):
        """MAIL_USERNAME 미설정 시 email_sent=False 반환."""
        resp = _register(p1_client)
        assert resp.status_code == 201
        data = resp.json()
        assert "email_sent" in data
        assert data["email_sent"] is False

    def test_register_with_mail_success_returns_email_sent_true(self, p1_client):
        """MAIL_USERNAME 설정 + 발송 성공 시 email_sent=True 반환."""
        mock_mail_settings = MagicMock()
        mock_mail_settings.MAIL_USERNAME = "test@gmail.com"
        mock_mail_settings.FRONTEND_URL = "http://localhost:3000"

        with patch("app.api.v1.auth.settings", mock_mail_settings), \
             patch("app.services.email_service.send_verification_email", new_callable=AsyncMock):
            resp = p1_client.post("/api/v1/auth/register", json={
                "email": "mailtest@example.com",
                "password": TEST_VALID_PASSWORD,
                "nickname": "테스터",
                "terms_agreed": True,
            })

        assert resp.status_code == 201
        assert resp.json()["email_sent"] is True

    def test_register_with_mail_failure_returns_email_sent_false(self, p1_client):
        """MAIL_USERNAME 설정 + 발송 실패 시 email_sent=False, 회원가입은 성공(201)."""
        mock_mail_settings = MagicMock()
        mock_mail_settings.MAIL_USERNAME = "test@gmail.com"
        mock_mail_settings.FRONTEND_URL = "http://localhost:3000"

        with patch("app.api.v1.auth.settings", mock_mail_settings), \
             patch("app.services.email_service.send_verification_email",
                   new_callable=AsyncMock,
                   side_effect=Exception("SMTP 연결 실패")):
            resp = p1_client.post("/api/v1/auth/register", json={
                "email": "failmail@example.com",
                "password": TEST_VALID_PASSWORD,
                "nickname": "테스터",
                "terms_agreed": True,
            })

        assert resp.status_code == 201
        data = resp.json()
        assert data["email_sent"] is False
        # 회원가입 자체는 성공 — 기본 필드 포함 확인
        assert data["email"] == "failmail@example.com"
        assert "id" in data

    def test_register_response_contains_all_user_fields(self, p1_client):
        """회원가입 응답에 기존 UserResponse 필드가 모두 포함됨 (하위 호환)."""
        resp = _register(p1_client)
        assert resp.status_code == 201
        data = resp.json()
        required_fields = ["id", "email", "membership_tier", "is_active", "is_admin",
                           "is_verified", "created_at", "email_sent"]
        for field in required_fields:
            assert field in data, f"필드 누락: {field}"

    def test_register_is_verified_false_when_mail_enabled(self, p1_client):
        """MAIL_USERNAME 설정 시 is_verified=False로 가입됨."""
        mock_mail_settings = MagicMock()
        mock_mail_settings.MAIL_USERNAME = "test@gmail.com"
        mock_mail_settings.FRONTEND_URL = "http://localhost:3000"

        with patch("app.api.v1.auth.settings", mock_mail_settings), \
             patch("app.services.email_service.send_verification_email", new_callable=AsyncMock):
            resp = p1_client.post("/api/v1/auth/register", json={
                "email": "unverified@example.com",
                "password": TEST_VALID_PASSWORD,
                "terms_agreed": True,
            })

        assert resp.status_code == 201
        assert resp.json()["is_verified"] is False
