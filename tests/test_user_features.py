"""
회원 기능 통합 테스트 (인증·프로필·즐겨찾기·히스토리·멤버십·공지·문의)
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

TEST_VALID_PASSWORD = "Abcd1234!@"
TEST_VALID_PASSWORD_2 = "Abcd1234#$"


# ── 픽스처 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def user_engine():
    from app.db.database import Base
    import app.db.models  # noqa: F401 — registers all ORM models in metadata
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture
def user_client(user_engine):
    from app.main import app
    from app.db.database import get_db

    Session = sessionmaker(autocommit=False, autoflush=False, bind=user_engine)

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


@pytest.fixture(autouse=True)
def reset_popular_week_cache():
    from app.api.v1 import users as users_module
    users_module._popular_week_cache["value"] = None
    users_module._popular_week_cache["expires_at"] = None
    yield
    users_module._popular_week_cache["value"] = None
    users_module._popular_week_cache["expires_at"] = None


def _register(client, email="test@example.com", password=TEST_VALID_PASSWORD, nickname="테스터"):
    return client.post("/api/v1/auth/register", json={
        "email": email, "password": password, "nickname": nickname, "terms_agreed": True,
    })


def _login(client, email="test@example.com", password=TEST_VALID_PASSWORD):
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json().get("access_token"), resp.json().get("refresh_token")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _subscribe_and_activate_pro(client, headers, pg_tx="pg_tx_user_features"):
    resp = client.post(
        "/api/v1/membership/subscribe",
        json={"plan": "pro"},
        headers=headers,
    )
    assert resp.status_code == 200
    payment_id = resp.json()["payment_id"]

    with patch("app.api.v1.membership.settings.ENV", "development"), \
         patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret"), \
         patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
        webhook_resp = client.post(
            "/api/v1/membership/webhook",
            json={
                "payment_id": payment_id,
                "pg_transaction_id": pg_tx,
                "status": "paid",
                "amount": 9900,
            },
            headers={"X-Webhook-Secret": "test-secret"},
        )
    assert webhook_resp.status_code == 200
    return payment_id


def _make_admin(user_engine, email="test@example.com"):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == email).first()
    user.is_admin = True
    db.commit()
    db.close()


# ── 인증 ──────────────────────────────────────────────────────────────────────

def test_register_success(user_client):
    resp = _register(user_client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "test@example.com"
    assert data["membership_tier"] == "free"
    assert data["is_admin"] is False


def test_register_duplicate_email_returns_409(user_client):
    _register(user_client)
    resp = _register(user_client)
    assert resp.status_code == 409


def test_register_terms_not_agreed_returns_422(user_client):
    resp = user_client.post("/api/v1/auth/register", json={
        "email": "a@example.com", "password": TEST_VALID_PASSWORD, "terms_agreed": False,
    })
    assert resp.status_code == 422


def test_register_short_password_returns_422(user_client):
    for password in ["1234", "Abcd123!@"]:
        resp = user_client.post("/api/v1/auth/register", json={
            "email": "a@example.com", "password": password, "terms_agreed": True,
        })
        assert resp.status_code == 422


def test_login_success(user_client):
    _register(user_client)
    resp = user_client.post("/api/v1/auth/login", json={
        "email": "test@example.com", "password": TEST_VALID_PASSWORD,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


def test_login_wrong_password_returns_401(user_client):
    _register(user_client)
    resp = user_client.post("/api/v1/auth/login", json={
        "email": "test@example.com", "password": "wrongpassword",
    })
    assert resp.status_code == 401


def test_login_unknown_email_returns_401(user_client):
    resp = user_client.post("/api/v1/auth/login", json={
        "email": "nobody@example.com", "password": TEST_VALID_PASSWORD,
    })
    assert resp.status_code == 401


def test_refresh_token_returns_new_access_token(user_client):
    _register(user_client)
    _, refresh_token = _login(user_client)
    resp = user_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_refresh_with_invalid_token_returns_401(user_client):
    resp = user_client.post("/api/v1/auth/refresh", json={"refresh_token": "invalid.token.here"})
    assert resp.status_code == 401


def test_logout_invalidates_refresh_token(user_client):
    _register(user_client)
    _, refresh_token = _login(user_client)

    resp = user_client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert resp.status_code == 204

    resp2 = user_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp2.status_code == 401


# ── 회원 정보 ─────────────────────────────────────────────────────────────────

def test_get_me_returns_user_info(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/users/me", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["email"] == "test@example.com"


def test_get_me_without_token_returns_401(user_client):
    resp = user_client.get("/api/v1/users/me")
    assert resp.status_code == 401


def test_update_nickname(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.patch("/api/v1/users/me", json={"nickname": "새닉네임"}, headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["nickname"] == "새닉네임"


def test_change_password_success(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.patch("/api/v1/users/me", json={
        "current_password": TEST_VALID_PASSWORD, "new_password": TEST_VALID_PASSWORD_2,
    }, headers=_auth(token))
    assert resp.status_code == 200

    # 새 비밀번호로 로그인 가능
    resp2 = user_client.post("/api/v1/auth/login", json={
        "email": "test@example.com", "password": TEST_VALID_PASSWORD_2,
    })
    assert resp2.status_code == 200


def test_change_password_wrong_current_returns_400(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.patch("/api/v1/users/me", json={
        "current_password": "wrongpassword", "new_password": TEST_VALID_PASSWORD_2,
    }, headers=_auth(token))
    assert resp.status_code == 400


def test_delete_account_deactivates_user(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.delete("/api/v1/users/me", headers=_auth(token))
    assert resp.status_code == 204

    # 비활성화 후 로그인 불가
    resp2 = user_client.post("/api/v1/auth/login", json={
        "email": "test@example.com", "password": TEST_VALID_PASSWORD,
    })
    assert resp2.status_code == 401


def test_get_usage(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/users/me/usage", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["membership_tier"] == "free"
    assert data["watchlist_count"] == 0


# ── 즐겨찾기 ──────────────────────────────────────────────────────────────────

def test_watchlist_add_and_list(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post("/api/v1/users/me/watchlist",
                            json={"stock_code": "005930", "company_name": "삼성전자"},
                            headers=headers)
    assert resp.status_code == 201

    resp2 = user_client.get("/api/v1/users/me/watchlist", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1
    assert resp2.json()[0]["stock_code"] == "005930"


def test_watchlist_duplicate_returns_409(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)
    payload = {"stock_code": "005930", "company_name": "삼성전자"}

    user_client.post("/api/v1/users/me/watchlist", json=payload, headers=headers)
    resp = user_client.post("/api/v1/users/me/watchlist", json=payload, headers=headers)
    assert resp.status_code == 409


def test_watchlist_delete(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    user_client.post("/api/v1/users/me/watchlist",
                     json={"stock_code": "005930", "company_name": "삼성전자"}, headers=headers)
    resp = user_client.delete("/api/v1/users/me/watchlist/005930", headers=headers)
    assert resp.status_code == 204

    resp2 = user_client.get("/api/v1/users/me/watchlist", headers=headers)
    assert resp2.json() == []


def test_watchlist_delete_nonexistent_returns_404(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.delete("/api/v1/users/me/watchlist/999999", headers=_auth(token))
    assert resp.status_code == 404


def test_watchlist_memo_update(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    user_client.post(
        "/api/v1/users/me/watchlist",
        json={"stock_code": "005930", "company_name": "Samsung Electronics"},
        headers=headers,
    )

    resp = user_client.patch(
        "/api/v1/users/me/watchlist/005930",
        json={"memo": "Long-term holding"},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["memo"] == "Long-term holding"


def test_watchlist_memo_clear(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    user_client.post(
        "/api/v1/users/me/watchlist",
        json={"stock_code": "005930", "company_name": "Samsung Electronics"},
        headers=headers,
    )
    user_client.patch(
        "/api/v1/users/me/watchlist/005930",
        json={"memo": "Temporary memo"},
        headers=headers,
    )

    resp = user_client.patch(
        "/api/v1/users/me/watchlist/005930",
        json={"memo": None},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["memo"] is None


def test_watchlist_memo_not_found(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.patch(
        "/api/v1/users/me/watchlist/999999",
        json={"memo": "Missing item"},
        headers=headers,
    )

    assert resp.status_code == 404


def test_get_watchlist_summaries_mixed(user_client, user_engine):
    from sqlalchemy.orm import sessionmaker
    from app.db.models import User, Watchlist, FinancialStatement, CollectionTask

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    db.add(Watchlist(user_id=user.id, stock_code="005930", company_name="Samsung Electronics"))
    db.add(FinancialStatement(
        company_id="005930",
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=300_000_000_000,
        operating_profit=30_000_000_000,
        net_income=25_000_000_000,
        total_assets=500_000_000_000,
        total_liabilities=200_000_000_000,
        equity=300_000_000_000,
    ))
    db.add(Watchlist(user_id=user.id, stock_code="000660", company_name="SK hynix"))
    db.add(CollectionTask(stock_code="000660", status="processing"))
    db.add(Watchlist(user_id=user.id, stock_code="035420", company_name="NAVER"))
    db.commit()
    db.close()

    resp = user_client.get("/api/v1/users/me/watchlist/summaries", headers=headers)

    assert resp.status_code == 200
    data = {item["stock_code"]: item for item in resp.json()}
    assert data["005930"]["status"] == "ready"
    assert data["005930"]["revenue"] == 300_000_000_000
    assert data["000660"]["status"] == "collecting"
    assert data["035420"]["status"] == "none"


def test_watchlist_free_tier_limit_returns_403(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    for i in range(5):
        resp = user_client.post(
            "/api/v1/users/me/watchlist",
            json={"stock_code": f"00593{i}", "company_name": f"기업{i}"},
            headers=headers,
        )
        assert resp.status_code == 201

    resp = user_client.post(
        "/api/v1/users/me/watchlist",
        json={"stock_code": "005935", "company_name": "기업5"},
        headers=headers,
    )
    assert resp.status_code == 403


# ── 멤버십 ────────────────────────────────────────────────────────────────────

def test_watchlist_add_triggers_collect_when_no_data(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    with patch("app.api.v1.users.DartService._init_failed", False), \
         patch("app.api.v1.users.DartService._stock_corps", {"005930": object()}), \
         patch("app.api.v1.users.DartService.fetch_and_process_data") as mock_collect:
        resp = user_client.post(
            "/api/v1/users/me/watchlist",
            json={"stock_code": "005930", "company_name": "Samsung Electronics"},
            headers=headers,
        )

    assert resp.status_code == 201
    mock_collect.assert_called_once_with("005930")


def test_watchlist_add_skips_collect_when_completed(user_client, user_engine):
    from app.db.models import CollectionTask
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(CollectionTask(
        stock_code="005930",
        status="completed",
        started_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.close()

    with patch("app.api.v1.users.DartService._init_failed", False), \
         patch("app.api.v1.users.DartService._stock_corps", {"005930": object()}), \
         patch("app.api.v1.users.DartService.fetch_and_process_data") as mock_collect:
        resp = user_client.post(
            "/api/v1/users/me/watchlist",
            json={"stock_code": "005930", "company_name": "Samsung Electronics"},
            headers=headers,
        )

    assert resp.status_code == 201
    mock_collect.assert_not_called()


def test_membership_days_until_expiry_within_7_days(user_client, user_engine):
    from sqlalchemy.orm import sessionmaker
    from app.db.models import UserMembership

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    me_resp = user_client.get("/api/v1/users/me", headers=headers)
    user_id = me_resp.json()["id"]

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(UserMembership(
        user_id=user_id,
        plan="pro",
        status="active",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        warning_sent=False,
    ))
    db.commit()
    db.close()

    resp = user_client.get("/api/v1/membership/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["days_until_expiry"] is not None
    assert 0 <= data["days_until_expiry"] <= 5


def test_membership_days_until_expiry_none_for_free(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.get("/api/v1/membership/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["days_until_expiry"] is None


def test_membership_warning_sent_set_when_expiring_soon(user_client, user_engine):
    from sqlalchemy.orm import sessionmaker
    from app.db.models import UserMembership

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    me_resp = user_client.get("/api/v1/users/me", headers=headers)
    user_id = me_resp.json()["id"]

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(UserMembership(
        user_id=user_id,
        plan="pro",
        status="active",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        warning_sent=False,
    ))
    db.commit()
    db.close()

    user_client.get("/api/v1/membership/me", headers=headers)

    db = Session()
    membership = db.query(UserMembership).filter(UserMembership.user_id == user_id).first()
    assert membership.warning_sent is True
    db.close()


def test_get_plans(user_client):
    resp = user_client.get("/api/v1/membership/plans")
    assert resp.status_code == 200
    plans = resp.json()
    assert len(plans) >= 1
    assert plans[0]["plan"] == "pro"


def test_get_membership_default_is_free(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/membership/me", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan"] == "free"
    assert data["watchlist_count"] == 0
    assert data["watchlist_limit"] == 5


def test_subscribe_creates_payment_record(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.post("/api/v1/membership/subscribe",
                            json={"plan": "pro"}, headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "payment_id" in data
    assert data["amount"] == 9900
    assert data["plan"] == "pro"


def test_subscribe_upgrades_tier_to_pro(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    _subscribe_and_activate_pro(user_client, headers)

    resp = user_client.get("/api/v1/membership/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["plan"] == "pro"


def test_webhook_rejects_invalid_secret_without_side_effects(user_client, user_engine):
    from app.db.models import PaymentRecord, User, UserMembership
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)
    subscribe_resp = user_client.post(
        "/api/v1/membership/subscribe",
        json={"plan": "pro"},
        headers=headers,
    )
    payment_id = subscribe_resp.json()["payment_id"]

    attempts = [None, "wrong-secret", ""]
    with patch("app.api.v1.membership.settings.ENV", "development"), \
         patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "valid-secret"):
        for received in attempts:
            webhook_headers = {}
            if received is not None:
                webhook_headers["X-Webhook-Secret"] = received
            resp = user_client.post(
                "/api/v1/membership/webhook",
                json={
                    "payment_id": payment_id,
                    "pg_transaction_id": "pg_tx_rejected",
                    "status": "paid",
                    "amount": 9900,
                },
                headers=webhook_headers,
            )
            assert resp.status_code == 401

    Session = sessionmaker(bind=user_engine)
    db = Session()
    try:
        record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == payment_id).first()
        user = db.query(User).filter(User.email == "test@example.com").first()
        memberships = db.query(UserMembership).filter(UserMembership.user_id == user.id).all()
        assert record.status == "pending"
        assert user.membership_tier == "free"
        assert memberships == []
    finally:
        db.close()


def test_webhook_unknown_payment_id_with_valid_secret_returns_404(user_client, user_engine):
    from app.db.models import UserMembership
    from sqlalchemy.orm import sessionmaker

    with patch("app.api.v1.membership.settings.ENV", "development"), \
         patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "valid-secret"):
        resp = user_client.post(
            "/api/v1/membership/webhook",
            json={
                "payment_id": "chatdart_doesnotexist",
                "pg_transaction_id": "pg_tx_missing",
                "status": "paid",
                "amount": 9900,
            },
            headers={"X-Webhook-Secret": "valid-secret"},
        )

    assert resp.status_code == 404

    Session = sessionmaker(bind=user_engine)
    db = Session()
    try:
        assert db.query(UserMembership).count() == 0
    finally:
        db.close()


def test_webhook_duplicate_paid_creates_single_membership(user_client, user_engine):
    from app.db.models import PaymentRecord, User, UserMembership
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)
    subscribe_resp = user_client.post(
        "/api/v1/membership/subscribe",
        json={"plan": "pro"},
        headers=headers,
    )
    payment_id = subscribe_resp.json()["payment_id"]

    webhook_body = {
        "payment_id": payment_id,
        "pg_transaction_id": "pg_tx_first",
        "status": "paid",
        "amount": 9900,
    }
    with patch("app.api.v1.membership.settings.ENV", "development"), \
         patch("app.api.v1.membership.settings.WEBHOOK_SECRET", "valid-secret"), \
         patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
        first_resp = user_client.post(
            "/api/v1/membership/webhook",
            json=webhook_body,
            headers={"X-Webhook-Secret": "valid-secret"},
        )
        second_resp = user_client.post(
            "/api/v1/membership/webhook",
            json={**webhook_body, "pg_transaction_id": "pg_tx_second"},
            headers={"X-Webhook-Secret": "valid-secret"},
        )

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert "이미 처리" in second_resp.json()["message"]

    Session = sessionmaker(bind=user_engine)
    db = Session()
    try:
        user = db.query(User).filter(User.email == "test@example.com").first()
        memberships = db.query(UserMembership).filter(UserMembership.user_id == user.id).all()
        record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == payment_id).first()
        assert len(memberships) == 1
        assert record.status == "paid"
        assert record.pg_transaction_id == "pg_tx_first"
    finally:
        db.close()


def test_payment_history_after_subscribe(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    subscribe_resp = user_client.post(
        "/api/v1/membership/subscribe",
        json={"plan": "pro"},
        headers=headers,
    )
    assert subscribe_resp.status_code == 200

    resp = user_client.get("/api/v1/users/me/payments", headers=headers)
    assert resp.status_code == 200
    payments = resp.json()
    assert len(payments) == 1
    assert payments[0]["amount"] == 9900
    assert payments[0]["plan"] == "pro"


def test_cancel_subscription_maintains_pro_until_expiry(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    _subscribe_and_activate_pro(user_client, headers)

    resp_cancel = user_client.post("/api/v1/membership/cancel", headers=headers)
    assert resp_cancel.status_code == 200
    assert resp_cancel.json()["status"] == "cancelled"

    resp = user_client.get("/api/v1/membership/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["plan"] == "pro"
    assert resp.json()["status"] == "cancelled"


def test_pro_tier_watchlist_limit_is_higher(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    _subscribe_and_activate_pro(user_client, headers)

    for i in range(6):
        resp = user_client.post(
            "/api/v1/users/me/watchlist",
            json={"stock_code": f"00593{i}", "company_name": f"Company {i}"},
            headers=headers,
        )
        assert resp.status_code == 201


def test_usage_reflects_membership_tier(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.get("/api/v1/users/me/usage", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["membership_tier"] == "free"
    assert resp.json()["watchlist_limit"] == 5

    _subscribe_and_activate_pro(user_client, headers)

    resp2 = user_client.get("/api/v1/users/me/usage", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["membership_tier"] == "pro"
    assert resp2.json()["watchlist_limit"] == 50


def test_subscribe_invalid_plan_returns_400(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.post("/api/v1/membership/subscribe",
                            json={"plan": "invalid"}, headers=_auth(token))
    assert resp.status_code == 400


def test_cancel_without_membership_returns_400(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.post("/api/v1/membership/cancel", headers=_auth(token))
    assert resp.status_code == 400


def test_get_payments_returns_empty_initially(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/users/me/payments", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json() == []


# ── 공지사항 ──────────────────────────────────────────────────────────────────

def test_notice_list_empty(user_client):
    resp = user_client.get("/api/v1/notices")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


def test_create_notice_requires_admin(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.post("/api/v1/notices",
                            json={"title": "공지", "content": "내용"},
                            headers=_auth(token))
    assert resp.status_code == 403


def test_create_notice_as_admin(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=user_engine)
    db = Session()

    _register(user_client)
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.post("/api/v1/notices",
                            json={"title": "공지사항", "content": "공지 내용입니다."},
                            headers=_auth(token))
    assert resp.status_code == 201
    assert resp.json()["title"] == "공지사항"


def test_get_notice_detail(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)

    resp = user_client.post(
        "/api/v1/notices",
        json={"title": "Detail test", "content": "Content"},
        headers=_auth(token),
    )
    notice_id = resp.json()["id"]

    resp2 = user_client.get(f"/api/v1/notices/{notice_id}")

    assert resp2.status_code == 200
    assert resp2.json()["title"] == "Detail test"


def test_get_notice_detail_not_found(user_client):
    resp = user_client.get("/api/v1/notices/9999")

    assert resp.status_code == 404


def test_admin_update_notice(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post(
        "/api/v1/notices",
        json={"title": "Old title", "content": "Old content"},
        headers=headers,
    )
    notice_id = resp.json()["id"]

    resp2 = user_client.patch(
        f"/api/v1/notices/{notice_id}",
        json={"title": "Updated title"},
        headers=headers,
    )

    assert resp2.status_code == 200
    assert resp2.json()["title"] == "Updated title"
    assert resp2.json()["content"] == "Old content"


def test_non_admin_cannot_update_notice(user_client, user_engine):
    _register(user_client, email="admin2@example.com")
    _make_admin(user_engine, email="admin2@example.com")
    admin_token, _ = _login(user_client, email="admin2@example.com")
    resp = user_client.post(
        "/api/v1/notices",
        json={"title": "Notice", "content": "Content"},
        headers=_auth(admin_token),
    )
    notice_id = resp.json()["id"]

    _register(user_client, email="user2@example.com")
    user_token, _ = _login(user_client, email="user2@example.com")
    resp2 = user_client.patch(
        f"/api/v1/notices/{notice_id}",
        json={"title": "Blocked"},
        headers=_auth(user_token),
    )

    assert resp2.status_code == 403


def test_admin_delete_notice(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post(
        "/api/v1/notices",
        json={"title": "Delete notice", "content": "Content"},
        headers=headers,
    )
    notice_id = resp.json()["id"]

    resp2 = user_client.delete(f"/api/v1/notices/{notice_id}", headers=headers)

    assert resp2.status_code == 204

    resp3 = user_client.get(f"/api/v1/notices/{notice_id}")
    assert resp3.status_code == 404


def test_non_admin_cannot_delete_notice(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.delete("/api/v1/notices/1", headers=_auth(token))

    assert resp.status_code == 403


def test_admin_stats_requires_admin(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/stats", headers=_auth(token))

    assert resp.status_code == 403


def test_admin_stats_returns_data(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=user_engine)
    db = Session()

    _register(user_client)
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/admin/stats", headers=_auth(token))

    assert resp.status_code == 200
    data = resp.json()
    assert "total_users" in data
    assert "active_users" in data
    assert "today_searches" in data
    assert data["total_users"] >= 1


# ── 관리자 기능 ──────────────────────────────────────────────────────────────

def test_admin_stats_counts_failed_collection_tasks(user_client, user_engine):
    from app.db.models import CollectionTask
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    _make_admin(user_engine)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(CollectionTask(stock_code="005930", status="failed", message="failed"))
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/admin/stats", headers=_auth(token))

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["failed_stock_codes"], list)
    assert data["failed_collections"] == 1
    assert data["failed_stock_codes"] == ["005930"]


def test_admin_list_users(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/users", headers=_auth(token))

    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1
    assert data["items"][0]["email"] == "test@example.com"


def test_non_admin_cannot_list_users(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/users", headers=_auth(token))

    assert resp.status_code == 403


def test_admin_list_users_filter_by_tier(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/users?membership_tier=free", headers=_auth(token))

    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["membership_tier"] == "free"


def test_admin_list_users_filter_by_admin_flag(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    _register(user_client, email="normal@example.com")
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/users?is_admin=true", headers=_auth(token))

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["is_admin"] is True


def test_admin_list_users_pagination(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    _register(user_client, email="page2@example.com")
    _register(user_client, email="page3@example.com")
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/admin/users?page=1&size=2", headers=_auth(token))

    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 1
    assert data["size"] == 2
    assert data["total"] == 3
    assert len(data["items"]) == 2


def test_admin_force_collect_returns_202(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)

    with patch("app.api.v1.admin.DartService._init_failed", False), \
         patch("app.api.v1.admin.DartService._stock_corps", {"005930": object()}), \
         patch("app.api.v1.admin.DartService.fetch_and_process_data"):
        resp = user_client.post("/api/v1/admin/collect/005930", headers=_auth(token))

    assert resp.status_code == 202
    assert resp.json()["status"] == "collecting"
    assert resp.json()["stock_code"] == "005930"


def test_admin_force_collect_resets_failed_task(user_client, user_engine):
    from app.db.models import CollectionTask
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    _make_admin(user_engine)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(CollectionTask(stock_code="005930", status="failed", message="old failure"))
    db.commit()
    db.close()

    token, _ = _login(user_client)
    with patch("app.api.v1.admin.DartService._init_failed", False), \
         patch("app.api.v1.admin.DartService._stock_corps", {"005930": object()}), \
         patch("app.api.v1.admin.DartService.fetch_and_process_data"):
        resp = user_client.post("/api/v1/admin/collect/005930", headers=_auth(token))

    assert resp.status_code == 202

    db = Session()
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == "005930").first()
    assert task.status == "processing"
    assert task.message == "관리자 강제 재수집"
    db.close()


def test_non_admin_cannot_force_collect(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.post("/api/v1/admin/collect/005930", headers=_auth(token))

    assert resp.status_code == 403


def test_admin_refresh_stale_no_stale_tasks(user_client, user_engine):
    _register(user_client)
    _make_admin(user_engine)
    token, _ = _login(user_client)

    with patch("app.api.v1.admin.DartService._init_failed", False), \
         patch("app.api.v1.admin.DartService._stock_corps", {"005930": object()}):
        resp = user_client.post(
            "/api/v1/admin/collect/refresh-stale?stale_days=7",
            headers=_auth(token),
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["triggered"] == 0
    assert data["stock_codes"] == []


def test_admin_refresh_stale_triggers_old_tasks(user_client, user_engine):
    from app.db.models import CollectionTask
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    _make_admin(user_engine)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
    db.add(CollectionTask(
        stock_code="005930",
        status="completed",
        started_at=old_time,
        updated_at=old_time,
    ))
    db.commit()
    db.close()

    token, _ = _login(user_client)
    with patch("app.api.v1.admin.DartService._init_failed", False), \
         patch("app.api.v1.admin.DartService._stock_corps", {"005930": object()}), \
         patch("app.api.v1.admin._admin_force_collect"):
        resp = user_client.post(
            "/api/v1/admin/collect/refresh-stale?stale_days=7",
            headers=_auth(token),
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["triggered"] == 1
    assert data["stock_codes"] == ["005930"]

    db = Session()
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == "005930").first()
    assert task.status == "processing"
    db.close()


def test_admin_refresh_stale_skips_fresh_tasks(user_client, user_engine):
    from app.db.models import CollectionTask
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    _make_admin(user_engine)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    recent_time = datetime.now(timezone.utc) - timedelta(days=1)
    db.add(CollectionTask(
        stock_code="000660",
        status="completed",
        started_at=recent_time,
        updated_at=recent_time,
    ))
    db.commit()
    db.close()

    token, _ = _login(user_client)
    with patch("app.api.v1.admin.DartService._init_failed", False), \
         patch("app.api.v1.admin.DartService._stock_corps", {"000660": object()}):
        resp = user_client.post(
            "/api/v1/admin/collect/refresh-stale?stale_days=7",
            headers=_auth(token),
        )

    assert resp.status_code == 202
    assert resp.json()["triggered"] == 0


def test_companies_stability_filter(user_client, user_engine):
    from app.db.models import CollectionTask, FinancialStatement
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(CollectionTask(stock_code="005930", status="completed"))
    db.add(FinancialStatement(
        company_id="005930",
        company_name="Samsung Electronics",
        fiscal_year=2024,
        revenue=200,
        operating_profit=20,
        net_income=10,
        total_assets=200,
        total_liabilities=50,
        equity=100,
        cost_of_sales=100,
        gross_profit=100,
        sga=80,
        cash=10,
    ))
    db.commit()
    db.close()

    resp = user_client.get(
        "/api/v1/finance/companies",
        params={"stability_status": "우수"},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_companies_growth_filter(user_client, user_engine):
    from app.db.models import CollectionTask, FinancialStatement
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    db.add(CollectionTask(stock_code="005930", status="completed"))
    db.add(FinancialStatement(
        company_id="005930",
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=100,
        operating_profit=10,
        net_income=5,
        total_assets=100,
        total_liabilities=40,
        equity=80,
        cost_of_sales=50,
        gross_profit=50,
        sga=40,
        cash=10,
    ))
    db.add(FinancialStatement(
        company_id="005930",
        company_name="Samsung Electronics",
        fiscal_year=2024,
        revenue=200,
        operating_profit=20,
        net_income=10,
        total_assets=200,
        total_liabilities=50,
        equity=100,
        cost_of_sales=100,
        gross_profit=100,
        sga=80,
        cash=10,
    ))
    db.commit()
    db.close()

    resp = user_client.get(
        "/api/v1/finance/companies",
        params={"growth_status": "양호"},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── 조회 히스토리 ──────────────────────────────────────────────────────────────

def test_get_history_returns_empty_initially(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/users/me/history", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json() == []


def test_clear_history_returns_204(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.delete("/api/v1/users/me/history", headers=_auth(token))

    assert resp.status_code == 204


# ── 문의하기 ──────────────────────────────────────────────────────────────────

def test_create_and_get_inquiry(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post("/api/v1/users/me/inquiries",
                            json={"title": "문의합니다", "content": "내용입니다"},
                            headers=headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"

    resp2 = user_client.get("/api/v1/users/me/inquiries", headers=headers)
    assert len(resp2.json()) == 1


def test_get_inquiry_not_found_returns_404(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/users/me/inquiries/9999", headers=_auth(token))
    assert resp.status_code == 404


# ── 비밀번호 찾기 ──────────────────────────────────────────────────────────────

# -- Admin inquiry management --

def test_admin_list_all_inquiries(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    user_client.post(
        "/api/v1/users/me/inquiries",
        json={"title": "Test inquiry", "content": "Question"},
        headers=_auth(token),
    )

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.get("/api/v1/notices/admin/inquiries", headers=_auth(token))

    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_non_admin_cannot_list_inquiries(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.get("/api/v1/notices/admin/inquiries", headers=_auth(token))

    assert resp.status_code == 403


def test_admin_answer_inquiry(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post(
        "/api/v1/users/me/inquiries",
        json={"title": "Inquiry", "content": "Question"},
        headers=headers,
    )
    inquiry_id = resp.json()["id"]

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.patch(
        f"/api/v1/notices/admin/inquiries/{inquiry_id}/answer",
        json={"answer": "Answer submitted."},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "Answer submitted."
    assert data["status"] == "answered"
    assert data["answered_at"] is not None


def test_non_admin_cannot_answer(user_client):
    _register(user_client)
    token, _ = _login(user_client)

    resp = user_client.patch(
        "/api/v1/notices/admin/inquiries/1/answer",
        json={"answer": "Answer"},
        headers=_auth(token),
    )

    assert resp.status_code == 403


def test_answer_nonexistent_inquiry_returns_404(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    _register(user_client)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.patch(
        "/api/v1/notices/admin/inquiries/9999/answer",
        json={"answer": "Answer"},
        headers=_auth(token),
    )

    assert resp.status_code == 404


def test_answer_empty_text_returns_400(user_client, user_engine):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    _register(user_client)
    token, _ = _login(user_client)
    resp = user_client.post(
        "/api/v1/users/me/inquiries",
        json={"title": "Inquiry", "content": "Question"},
        headers=_auth(token),
    )
    inquiry_id = resp.json()["id"]

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    user.is_admin = True
    db.commit()
    db.close()

    token, _ = _login(user_client)
    resp = user_client.patch(
        f"/api/v1/notices/admin/inquiries/{inquiry_id}/answer",
        json={"answer": "   "},
        headers=_auth(token),
    )

    assert resp.status_code == 400


# -- Password reset --

def test_forgot_password_unknown_email_returns_204(user_client):
    resp = user_client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.status_code == 204


def test_forgot_and_reset_password(user_client):
    _register(user_client)

    with patch("app.api.v1.auth.secrets.token_urlsafe", return_value="fixed-test-token-abc123"), \
         patch("app.services.email_service.send_email"):
        resp = user_client.post("/api/v1/auth/forgot-password", json={"email": "test@example.com"})
        assert resp.status_code == 204

    # 새 비밀번호로 재설정
    resp2 = user_client.post("/api/v1/auth/reset-password", json={
        "token": "fixed-test-token-abc123",
        "new_password": TEST_VALID_PASSWORD_2,
    })
    assert resp2.status_code == 204

    # 새 비밀번호로 로그인 가능
    resp3 = user_client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": TEST_VALID_PASSWORD_2,
    })
    assert resp3.status_code == 200


def test_reset_password_invalid_token_returns_400(user_client):
    resp = user_client.post("/api/v1/auth/reset-password", json={
        "token": "nonexistent-token",
        "new_password": TEST_VALID_PASSWORD_2,
    })
    assert resp.status_code == 400


def test_reset_password_token_cannot_be_reused(user_client):
    _register(user_client)

    with patch("app.api.v1.auth.secrets.token_urlsafe", return_value="reuse-test-token"), \
         patch("app.services.email_service.send_email"):
        user_client.post("/api/v1/auth/forgot-password", json={"email": "test@example.com"})

    # 첫 번째 사용
    user_client.post("/api/v1/auth/reset-password", json={
        "token": "reuse-test-token",
        "new_password": TEST_VALID_PASSWORD_2,
    })

    # 동일 토큰 재사용 시 400
    resp = user_client.post("/api/v1/auth/reset-password", json={
        "token": "reuse-test-token",
        "new_password": TEST_VALID_PASSWORD_2,
    })
    assert resp.status_code == 400


def test_reset_password_short_password_returns_422(user_client):
    resp = user_client.post("/api/v1/auth/reset-password", json={
        "token": "some-token",
        "new_password": "short",
    })
    assert resp.status_code == 422


def test_reset_password_expired_token_returns_400(user_client, user_engine):
    from sqlalchemy.orm import sessionmaker
    from app.db.models import PasswordResetToken
    from app.core.security import hash_token
    from datetime import datetime, timedelta, timezone

    _register(user_client)

    with patch("app.api.v1.auth.secrets.token_urlsafe", return_value="expired-token-xyz"), \
         patch("app.services.email_service.send_email"):
        user_client.post("/api/v1/auth/forgot-password", json={"email": "test@example.com"})

    # DB에서 직접 만료 시간을 과거로 변경
    Session = sessionmaker(bind=user_engine)
    db = Session()
    token_hash = hash_token("expired-token-xyz")
    record = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()
    record.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    db.close()

    resp = user_client.post("/api/v1/auth/reset-password", json={
        "token": "expired-token-xyz",
        "new_password": TEST_VALID_PASSWORD_2,
    })
    assert resp.status_code == 400


def test_reset_password_invalidates_existing_session(user_client):
    _register(user_client)
    _, refresh_token = _login(user_client)

    with patch("app.api.v1.auth.secrets.token_urlsafe", return_value="session-invalidate-token"), \
         patch("app.services.email_service.send_email"):
        user_client.post("/api/v1/auth/forgot-password", json={"email": "test@example.com"})

    user_client.post("/api/v1/auth/reset-password", json={
        "token": "session-invalidate-token",
        "new_password": TEST_VALID_PASSWORD_2,
    })

    # 재설정 전 발급된 refresh_token으로 갱신 시도 → 401
    resp = user_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


def test_verify_email_success(user_client):
    with patch("app.api.v1.auth.settings") as mock_s, \
         patch("app.api.v1.auth.secrets.token_urlsafe", return_value="test-verify-token-xyz"), \
         patch("app.services.email_service.send_verification_email"):
        mock_s.MAIL_USERNAME = "test@smtp.com"
        mock_s.REQUIRE_EMAIL_VERIFICATION = False
        mock_s.FRONTEND_URL = "http://localhost:3000"

        resp = user_client.post("/api/v1/auth/register", json={
            "email": "verify@example.com",
            "password": TEST_VALID_PASSWORD,
            "terms_agreed": True,
        })
        assert resp.status_code == 201
        assert resp.json()["is_verified"] is False

    resp2 = user_client.post(
        "/api/v1/auth/verify-email",
        json={"token": "test-verify-token-xyz"},
    )

    assert resp2.status_code == 204


def test_verify_email_invalid_token_returns_400(user_client):
    resp = user_client.post(
        "/api/v1/auth/verify-email",
        json={"token": "nonexistent-token-xyz"},
    )

    assert resp.status_code == 400


def test_resend_verification_success(user_client):
    with patch("app.api.v1.auth.settings") as mock_s, \
         patch("app.services.email_service.send_verification_email"):
        mock_s.MAIL_USERNAME = "test@smtp.com"
        mock_s.REQUIRE_EMAIL_VERIFICATION = False
        mock_s.FRONTEND_URL = "http://localhost:3000"

        register_resp = user_client.post("/api/v1/auth/register", json={
            "email": "resend@example.com",
            "password": TEST_VALID_PASSWORD,
            "terms_agreed": True,
        })
        assert register_resp.status_code == 201

        resp = user_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "resend@example.com"},
        )

    assert resp.status_code == 204


def test_user_history_returns_list(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.get("/api/v1/users/me/history", headers=headers)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_watchlist_memo_max_length(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    user_client.post(
        "/api/v1/users/me/watchlist",
        json={"stock_code": "005930", "company_name": "Samsung Electronics"},
        headers=headers,
    )

    resp = user_client.patch(
        "/api/v1/users/me/watchlist/005930",
        json={"memo": "a" * 501},
        headers=headers,
    )

    assert resp.status_code == 422


def test_dashboard_basic(user_client, user_engine):
    """대시보드는 즐겨찾기·인기·최근검색 섹션과 사용량을 반환한다."""
    from sqlalchemy.orm import sessionmaker
    from app.db.models import User, Watchlist, FinancialStatement, SearchLog

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    Session = sessionmaker(bind=user_engine)
    db = Session()
    user = db.query(User).filter(User.email == "test@example.com").first()
    db.add(Watchlist(user_id=user.id, stock_code="005930", company_name="Samsung Electronics"))
    db.add(FinancialStatement(
        company_id="005930",
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=300_000_000_000,
        operating_profit=30_000_000_000,
        net_income=25_000_000_000,
        total_assets=500_000_000_000,
        total_liabilities=200_000_000_000,
        equity=300_000_000_000,
    ))
    db.add(SearchLog(
        stock_code="005930",
        company_name="Samsung Electronics",
        user_id=user.id,
        searched_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.close()

    resp = user_client.get("/api/v1/users/me/dashboard", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["membership_tier"] in ("free", "pro")
    assert data["watchlist_count"] == 1
    assert data["watchlist_limit"] >= 1
    assert len(data["watchlist_preview"]) == 1
    assert data["watchlist_preview"][0]["stock_code"] == "005930"
    assert any(item["stock_code"] == "005930" for item in data["popular_week"])
    assert any(item["stock_code"] == "005930" for item in data["recent_searches"])


def test_dashboard_unauthorized(user_client):
    """로그인 없이 대시보드 호출 시 401."""
    resp = user_client.get("/api/v1/users/me/dashboard")

    assert resp.status_code == 401


def test_dashboard_popular_week_cache_hits(user_client):
    """첫 호출 후 두 번째 대시보드 호출은 popular_week 캐시를 재사용한다."""
    from app.api.v1 import users as users_module

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp1 = user_client.get("/api/v1/users/me/dashboard", headers=headers)
    assert resp1.status_code == 200
    cached_value = users_module._popular_week_cache["value"]
    cached_expires = users_module._popular_week_cache["expires_at"]
    assert cached_value is not None
    assert cached_expires is not None

    resp2 = user_client.get("/api/v1/users/me/dashboard", headers=headers)
    assert resp2.status_code == 200
    assert users_module._popular_week_cache["value"] is cached_value


def test_dashboard_popular_week_cache_refreshes_after_expiry(user_client):
    """popular_week 캐시가 만료되면 새 값으로 갱신된다."""
    from app.api.v1 import users as users_module

    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    expired_value = []
    users_module._popular_week_cache["value"] = expired_value
    users_module._popular_week_cache["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    resp = user_client.get("/api/v1/users/me/dashboard", headers=headers)

    assert resp.status_code == 200
    assert users_module._popular_week_cache["value"] is not expired_value


def test_add_watchlist_includes_usage(user_client):
    _register(user_client)
    token, _ = _login(user_client)
    headers = _auth(token)

    resp = user_client.post(
        "/api/v1/users/me/watchlist",
        json={"stock_code": "005930", "company_name": "Samsung Electronics"},
        headers=headers,
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["item"]["stock_code"] == "005930"
    assert data["watchlist_count"] == 1
    assert data["watchlist_limit"] >= 1
