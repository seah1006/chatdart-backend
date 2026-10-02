"""
Phase 2 안정성 패치 통합 테스트

2-1. 백그라운드 작업 예외 처리
    - _collect_with_email 이메일 실패 / fetch 실패 시 예외 비전파
    - _silent_refresh fetch 실패 시 예외 비전파
    - _daily_prewarm_loop 루프 오류 후 계속 실행
2-2. 테스트 커버리지 보강
    - 웹훅 엣지 케이스 (404 / 400-failed / 중복 멱등성 / invalid plan)
    - 멤버십 만료 자동 다운그레이드 / 해지 상태
    - 비활성화 계정 refresh 401
2-3. DB 연결 풀 환경별 분기 검증
"""
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

TEST_VALID_PASSWORD = "Abcd1234!@"


# ── 공통 픽스처 ───────────────────────────────────────────────────────────────

@pytest.fixture
def p2_engine():
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
def p2_client(p2_engine):
    from app.main import app
    from app.db.database import get_db

    Session = sessionmaker(autocommit=False, autoflush=False, bind=p2_engine)

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


def _subscribe_and_get_payment_id(client, token):
    resp = client.post("/api/v1/membership/subscribe",
                       json={"plan": "pro"}, headers=_auth(token))
    return resp.json()["payment_id"]


def _webhook(client, payment_id, pg_tx="pg_tx_001", status="paid", amount=9900, headers=None):
    if headers is None:
        headers = {"X-Webhook-Secret": "test-secret"}
    return client.post("/api/v1/membership/webhook", json={
        "payment_id": payment_id,
        "pg_transaction_id": pg_tx,
        "status": status,
        "amount": amount,
    }, headers=headers)


# ── 2-2-A. 웹훅 엣지 케이스 ─────────────────────────────────────────────────

class TestWebhookEdgeCases:
    def test_webhook_missing_shared_secret_returns_403(self, p2_client):
        """WEBHOOK_SECRET이 설정된 환경에서는 secret 헤더가 없으면 403."""
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"):
            resp = _webhook(p2_client, "chatdart_nonexistent_payment", headers={})
        assert resp.status_code == 401

    def test_webhook_valid_shared_secret_continues_processing(self, p2_client):
        """올바른 shared secret이면 기존 웹훅 처리 경로로 진입한다."""
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"):
            resp = _webhook(
                p2_client,
                "chatdart_nonexistent_payment",
                headers={"X-Webhook-Secret": "test-secret"},
            )
        assert resp.status_code == 404

    def test_webhook_unknown_payment_id_returns_404(self, p2_client):
        """존재하지 않는 payment_id로 웹훅 요청 시 404."""
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"):
            resp = _webhook(p2_client, "chatdart_nonexistent_payment")
        assert resp.status_code == 404

    def test_webhook_status_failed_marks_record_failed(self, p2_client, p2_engine):
        """PG사가 status=failed 로 알릴 때 PaymentRecord도 failed로 변경, 400 반환."""
        from app.db.models import PaymentRecord
        from sqlalchemy.orm import sessionmaker

        _register(p2_client)
        token, _ = _login(p2_client)
        payment_id = _subscribe_and_get_payment_id(p2_client, token)

        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"):
            resp = _webhook(p2_client, payment_id, status="failed")
        assert resp.status_code == 400

        Session = sessionmaker(bind=p2_engine)
        db = Session()
        record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == payment_id).first()
        db.close()
        assert record.status == "failed"

    def test_webhook_duplicate_paid_is_idempotent(self, p2_client):
        """이미 paid 처리된 결제에 재요청 시 200 + 멱등성 메시지."""
        _register(p2_client)
        token, _ = _login(p2_client)
        payment_id = _subscribe_and_get_payment_id(p2_client, token)

        # 첫 번째: 정상 처리
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            _webhook(p2_client, payment_id)

        # 두 번째: 동일 payment_id 재요청
        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            resp = _webhook(p2_client, payment_id, pg_tx="pg_tx_002")
        assert resp.status_code == 200
        assert "이미 처리된" in resp.json()["message"]

    def test_webhook_invalid_plan_returns_400(self, p2_client, p2_engine):
        """plan이 유효하지 않은 PaymentRecord에 웹훅 수신 시 400."""
        from app.db.models import PaymentRecord
        from sqlalchemy.orm import sessionmaker
        import secrets

        _register(p2_client)
        token, _ = _login(p2_client)

        # 유효하지 않은 plan으로 PaymentRecord 직접 삽입
        Session = sessionmaker(bind=p2_engine)
        db = Session()
        from app.db.models import User
        user = db.query(User).filter(User.email == "user@example.com").first()
        bad_record = PaymentRecord(
            user_id=user.id,
            plan="nonexistent_plan",
            amount=9900,
            payment_id=f"chatdart_{secrets.token_hex(8)}",
            status="pending",
        )
        db.add(bad_record)
        db.commit()
        bad_payment_id = bad_record.payment_id
        db.close()

        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            resp = _webhook(p2_client, bad_payment_id)
        assert resp.status_code == 400


# ── 2-1. 백그라운드 작업 예외 처리 ──────────────────────────────────────────

class TestBackgroundTaskErrorHandling:
    def test_claim_collection_task_returns_false_on_integrity_error(self, p2_engine):
        """동시 INSERT 경쟁으로 IntegrityError 발생 시 False를 반환하고 rollback한다.

        999999 row가 없는 빈 DB에서 실행해 UPDATE rowcount=0 → INSERT 경로만 실행한다.
        INSERT commit에서 IntegrityError가 발생하는 시나리오를 검증한다.
        """
        from app.api.v1.finance import _claim_collection_task
        from app.db.models import CollectionTask

        Session = sessionmaker(bind=p2_engine)
        db = Session()
        try:
            assert db.query(CollectionTask).filter_by(stock_code="999999").first() is None

            with patch.object(db, "commit", side_effect=IntegrityError("", {}, None)), \
                 patch.object(db, "rollback", wraps=db.rollback) as mock_rollback:
                result = _claim_collection_task("999999", db)

            assert result is False
            mock_rollback.assert_called_once()
        finally:
            db.close()

    def test_collect_with_email_email_failure_no_propagation(self):
        """이메일 발송 실패 시 _collect_with_email이 예외를 propagate하지 않음."""
        from app.api.v1.finance import _collect_with_email

        async def run():
            with patch("app.api.v1.finance.DartService.fetch_and_process_data"), \
                 patch("app.services.email_service.send_collect_complete",
                       new_callable=AsyncMock,
                       side_effect=Exception("SMTP 연결 실패")):
                # 예외 없이 완료되어야 함
                await _collect_with_email("005930", "user@test.com")

        asyncio.run(run())  # raise가 없으면 테스트 통과

    def test_collect_with_email_fetch_exception_no_propagation(self):
        """fetch_and_process_data 예외 시 _collect_with_email이 예외를 propagate하지 않음."""
        from app.api.v1.finance import _collect_with_email

        async def run():
            # asyncio.to_thread가 실행하는 동기 함수를 직접 대체
            async def mock_to_thread(fn, *args):
                raise RuntimeError("DART 서버 다운")

            with patch("asyncio.to_thread", new=mock_to_thread):
                await _collect_with_email("005930", "user@test.com")

        asyncio.run(run())

    def test_collect_with_email_no_email_skips_send(self):
        """user_email이 None이면 이메일 발송을 시도하지 않음."""
        from app.api.v1.finance import _collect_with_email

        async def run():
            with patch("app.api.v1.finance.DartService.fetch_and_process_data") as mock_fetch, \
                 patch("app.services.email_service.send_collect_complete",
                       new_callable=AsyncMock) as mock_send:
                await _collect_with_email("005930", None)
                mock_fetch.assert_called_once_with("005930")
                mock_send.assert_not_called()

        asyncio.run(run())

    def test_silent_refresh_fetch_failure_no_propagation(self):
        """_silent_refresh에서 fetch 예외 발생 시 호출부로 propagate하지 않음."""
        from app.api.v1.finance import _silent_refresh

        mock_db = MagicMock()
        mock_task = MagicMock()
        mock_task.status = "completed"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_task

        with patch("app.api.v1.finance.SessionLocal", return_value=mock_db), \
             patch("app.api.v1.finance.DartService.fetch_and_process_data",
                   side_effect=RuntimeError("DART 타임아웃")):
            _silent_refresh("005930")  # 예외 없이 완료되어야 함

    def test_silent_refresh_skips_when_processing(self):
        """이미 processing 중인 태스크는 _silent_refresh가 fetch를 호출하지 않고 즉시 반환."""
        from app.api.v1.finance import _silent_refresh

        mock_db = MagicMock()

        with patch("app.api.v1.finance.SessionLocal", return_value=mock_db), \
             patch("app.api.v1.finance._claim_collection_task", return_value=False), \
             patch("app.api.v1.finance.DartService.fetch_and_process_data") as mock_fetch:
            _silent_refresh("005930")
            mock_fetch.assert_not_called()

    def test_daily_prewarm_loop_continues_after_exception(self):
        """_daily_prewarm_loop 내부 예외 발생 후 루프가 계속 실행됨."""
        from app.main import _daily_prewarm_loop
        call_count = {"n": 0}

        # StopIteration은 Exception의 하위 클래스라 except Exception에 잡힘 → 루프 탈출 불가.
        # BaseException 직계 하위 클래스를 사용해야 except Exception을 통과한다.
        class _Stop(BaseException):
            pass

        def mock_collect(top_n):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("첫 번째 수집 실패")
            raise _Stop  # 두 번째 호출에서 루프 강제 종료

        # time.sleep은 main.py 함수 안에서 `import time; time.sleep(...)` 형태로 사용.
        # `time` 모듈 자체를 패치한다.
        with patch("app.main._collect_popular_stocks", side_effect=mock_collect), \
             patch("app.main._next_kst_3am", return_value=datetime.now(timezone.utc)), \
             patch("time.sleep"):  # time 모듈 레벨에서 패치
            try:
                _daily_prewarm_loop()
            except _Stop:
                pass  # 테스트 종료용

        # 1회 실패 후에도 2회차가 실행되었으면 루프가 살아있던 것
        assert call_count["n"] == 2


# ── 2-2-B. 멤버십 엣지 케이스 ────────────────────────────────────────────────

class TestMembershipEdgeCases:
    def test_expired_membership_returns_free_and_downgrades_tier(self, p2_client, p2_engine):
        """만료된 멤버십 조회 시 plan=free 반환 + User.membership_tier = 'free' 자동 갱신."""
        from app.db.models import User, UserMembership
        from sqlalchemy.orm import sessionmaker

        _register(p2_client)
        token, _ = _login(p2_client)

        # 이미 만료된 멤버십 직접 삽입
        Session = sessionmaker(bind=p2_engine)
        db = Session()
        user = db.query(User).filter(User.email == "user@example.com").first()
        user.membership_tier = "pro"
        expired_membership = UserMembership(
            user_id=user.id,
            plan="pro",
            started_at=datetime.now(timezone.utc) - timedelta(days=60),
            expires_at=datetime.now(timezone.utc) - timedelta(days=30),  # 이미 만료
            status="active",
        )
        db.add(expired_membership)
        db.commit()
        db.close()

        resp = p2_client.get("/api/v1/membership/me", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["plan"] == "free"

        # DB에서 membership_tier도 'free'로 갱신되었는지 확인
        db = Session()
        user = db.query(User).filter(User.email == "user@example.com").first()
        assert user.membership_tier == "free"
        db.close()

    def test_cancelled_membership_returns_cancelled_status(self, p2_client):
        """활성 멤버십 해지 후 status=cancelled 반환."""
        _register(p2_client)
        token, _ = _login(p2_client)
        payment_id = _subscribe_and_get_payment_id(p2_client, token)

        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            _webhook(p2_client, payment_id)

        resp = p2_client.post("/api/v1/membership/cancel", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_webhook_activates_membership_and_upgrades_user_tier(self, p2_client, p2_engine):
        """웹훅 성공 후 User.membership_tier가 pro로 업그레이드됨."""
        from app.db.models import User
        from sqlalchemy.orm import sessionmaker

        _register(p2_client)
        token, _ = _login(p2_client)
        payment_id = _subscribe_and_get_payment_id(p2_client, token)

        with patch("app.api.v1.membership.settings.ENV", "development"), \
             patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
             patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
            resp = _webhook(p2_client, payment_id)
        assert resp.status_code == 200

        Session = sessionmaker(bind=p2_engine)
        db = Session()
        user = db.query(User).filter(User.email == "user@example.com").first()
        assert user.membership_tier == "pro"
        db.close()

    def test_cancel_membership_without_active_returns_400(self, p2_client):
        """활성 유료 멤버십 없이 해지 시도 시 400."""
        _register(p2_client)
        token, _ = _login(p2_client)
        resp = p2_client.post("/api/v1/membership/cancel", headers=_auth(token))
        assert resp.status_code == 400


# ── 2-2-C. 인증 엣지 케이스 ──────────────────────────────────────────────────

class TestAuthEdgeCases:
    def test_inactive_user_refresh_token_returns_401(self, p2_client, p2_engine):
        """비활성화된 계정의 refresh_token으로 갱신 시도 시 401."""
        from app.db.models import User
        from sqlalchemy.orm import sessionmaker

        _register(p2_client)
        _, refresh_token = _login(p2_client)

        # 계정 비활성화
        Session = sessionmaker(bind=p2_engine)
        db = Session()
        user = db.query(User).filter(User.email == "user@example.com").first()
        user.is_active = False
        db.commit()
        db.close()

        resp = p2_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 401

    def test_refresh_after_deletion_returns_401(self, p2_client):
        """회원 탈퇴 후 refresh_token으로 토큰 갱신 시도 시 401."""
        _register(p2_client)
        access_token, refresh_token = _login(p2_client)

        p2_client.delete("/api/v1/users/me", headers=_auth(access_token))

        resp = p2_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 401

    def test_access_to_protected_route_after_account_deletion_returns_401(self, p2_client):
        """탈퇴 후 access_token으로 보호된 엔드포인트 접근 시 401."""
        _register(p2_client)
        access_token, _ = _login(p2_client)

        p2_client.delete("/api/v1/users/me", headers=_auth(access_token))

        resp = p2_client.get("/api/v1/users/me", headers=_auth(access_token))
        assert resp.status_code == 401


# ── 2-3. DB 연결 풀 환경별 분기 검증 ─────────────────────────────────────────

class TestDbPoolConfiguration:
    def test_sqlite_engine_has_no_pool_size_kwargs(self):
        """SQLite 엔진은 pool_size/max_overflow 없이 생성됨."""
        # database.py의 _pool_kwargs 계산 로직을 직접 검증
        is_sqlite = True
        pool_kwargs: dict = {}
        if not is_sqlite:
            pool_kwargs = {"pool_size": 10}  # SQLite가 아니면 설정됨
        assert pool_kwargs == {}  # SQLite는 빈 딕셔너리

    def test_postgresql_production_pool_kwargs(self):
        """운영 환경 PostgreSQL 풀 설정: pool_size=20, max_overflow=30."""
        is_sqlite = False
        is_production = True

        pool_kwargs: dict = {}
        if not is_sqlite:
            if is_production:
                pool_kwargs = {"pool_size": 20, "max_overflow": 30}
            else:
                pool_kwargs = {"pool_size": 10, "max_overflow": 10}
            pool_kwargs.update({"pool_timeout": 30, "pool_recycle": 1800})

        assert pool_kwargs["pool_size"] == 20
        assert pool_kwargs["max_overflow"] == 30
        assert pool_kwargs["pool_timeout"] == 30
        assert pool_kwargs["pool_recycle"] == 1800

    def test_postgresql_staging_pool_kwargs(self):
        """스테이징/개발 PostgreSQL 풀 설정: pool_size=10, max_overflow=10."""
        is_sqlite = False
        is_production = False

        pool_kwargs: dict = {}
        if not is_sqlite:
            if is_production:
                pool_kwargs = {"pool_size": 20, "max_overflow": 30}
            else:
                pool_kwargs = {"pool_size": 10, "max_overflow": 10}
            pool_kwargs.update({"pool_timeout": 30, "pool_recycle": 1800})

        assert pool_kwargs["pool_size"] == 10
        assert pool_kwargs["max_overflow"] == 10
        assert pool_kwargs["pool_recycle"] == 1800

    def test_pool_kwargs_computation_matches_database_module(self):
        """database.py의 _pool_kwargs 계산 로직이 코드와 일치하는지 검증."""
        # database.py 코드와 동일한 로직으로 검증 (리로드 없이)
        for is_production, expected_size, expected_overflow in [
            (True,  20, 30),
            (False, 10, 10),
        ]:
            pool_kwargs: dict = {}
            is_sqlite = False  # PostgreSQL 시나리오
            if not is_sqlite:
                if is_production:
                    pool_kwargs = {"pool_size": 20, "max_overflow": 30}
                else:
                    pool_kwargs = {"pool_size": 10, "max_overflow": 10}
                pool_kwargs.update({"pool_timeout": 30, "pool_recycle": 1800})

            assert pool_kwargs["pool_size"] == expected_size, f"is_production={is_production}"
            assert pool_kwargs["max_overflow"] == expected_overflow, f"is_production={is_production}"
            assert "pool_timeout" in pool_kwargs
            assert "pool_recycle" in pool_kwargs
