import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def webhook_engine():
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
def webhook_client(webhook_engine):
    from app.db.database import get_db
    from app.main import app

    Session = sessionmaker(autocommit=False, autoflush=False, bind=webhook_engine)

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
        with TestClient(app) as client:
            yield client
    app.dependency_overrides.clear()


def _create_payment(engine, payment_id="chatdart_hmac_test"):
    from app.core.security import hash_password
    from app.db.models import PaymentRecord, User

    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(
        email=f"{payment_id}@example.com",
        hashed_password=hash_password("Abcd1234!@"),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.flush()
    db.add(PaymentRecord(
        user_id=user.id,
        plan="pro",
        amount=9900,
        payment_id=payment_id,
        status="pending",
    ))
    db.commit()
    db.close()
    return payment_id


def _raw_webhook_body(payment_id: str, pg_tx="pg_tx_hmac", status="paid") -> bytes:
    return json.dumps(
        {
            "payment_id": payment_id,
            "pg_transaction_id": pg_tx,
            "status": status,
            "amount": 9900,
        },
        separators=(",", ":"),
    ).encode()


def _portone_secret() -> tuple[str, bytes]:
    secret_bytes = b"portone-integration-secret"
    return "whsec_" + base64.b64encode(secret_bytes).decode(), secret_bytes


def _portone_headers(raw_body: bytes, secret_bytes: bytes, timestamp: int | None = None) -> dict[str, str]:
    timestamp_value = str(timestamp or int(time.time()))
    webhook_id = "webhook-integration-id"
    message = f"{webhook_id}.{timestamp_value}.".encode() + raw_body
    signature = base64.b64encode(hmac.new(secret_bytes, message, hashlib.sha256).digest()).decode()
    return {
        "content-type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp_value,
        "webhook-signature": f"v1,{signature}",
    }


def _post_raw_webhook(client, raw_body: bytes, headers: dict[str, str]):
    return client.post("/api/v1/membership/webhook", content=raw_body, headers=headers)


def test_webhook_production_requires_hmac(webhook_client, webhook_engine, monkeypatch):
    payment_id = _create_payment(webhook_engine, "chatdart_requires_hmac")
    raw_body = _raw_webhook_body(payment_id)
    monkeypatch.setattr("app.api.v1.membership.settings.ENV", "production")

    resp = _post_raw_webhook(webhook_client, raw_body, {"X-Webhook-Secret": "test-secret"})
    assert resp.status_code == 401


def test_webhook_production_hmac_valid_succeeds(webhook_client, webhook_engine, monkeypatch):
    payment_id = _create_payment(webhook_engine, "chatdart_hmac_valid")
    raw_body = _raw_webhook_body(payment_id)
    secret, secret_bytes = _portone_secret()
    monkeypatch.setattr("app.api.v1.membership.settings.ENV", "production")
    monkeypatch.setattr("app.services.pg_service.settings.PG_PROVIDER", "portone")
    monkeypatch.setattr("app.services.pg_service.settings.PORTONE_WEBHOOK_SECRET", secret)

    with patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
        resp = _post_raw_webhook(webhook_client, raw_body, _portone_headers(raw_body, secret_bytes))

    assert resp.status_code == 200


def test_webhook_production_hmac_invalid_fails(webhook_client, webhook_engine, monkeypatch):
    from app.db.models import PaymentRecord

    payment_id = _create_payment(webhook_engine, "chatdart_hmac_invalid")
    raw_body = _raw_webhook_body(payment_id)
    secret, _ = _portone_secret()
    monkeypatch.setattr("app.api.v1.membership.settings.ENV", "production")
    monkeypatch.setattr("app.services.pg_service.settings.PG_PROVIDER", "portone")
    monkeypatch.setattr("app.services.pg_service.settings.PORTONE_WEBHOOK_SECRET", secret)

    resp = _post_raw_webhook(webhook_client, raw_body, _portone_headers(raw_body, b"wrong-secret"))
    assert resp.status_code == 401

    Session = sessionmaker(bind=webhook_engine)
    db = Session()
    record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == payment_id).first()
    db.close()
    assert record.status == "pending"


def test_webhook_development_fallback_works(webhook_client, webhook_engine, monkeypatch):
    payment_id = _create_payment(webhook_engine, "chatdart_dev_fallback")
    raw_body = _raw_webhook_body(payment_id)
    monkeypatch.setattr("app.api.v1.membership.settings.ENV", "development")
    monkeypatch.setattr("app.api.v1.membership.settings.WEBHOOK_SECRET", "test-secret")

    with patch("app.api.v1.membership.verify_payment", new_callable=AsyncMock, return_value=True):
        resp = _post_raw_webhook(webhook_client, raw_body, {
            "content-type": "application/json",
            "X-Webhook-Secret": "test-secret",
        })

    assert resp.status_code == 200


def test_webhook_stale_timestamp_rejected(webhook_client, webhook_engine, monkeypatch):
    payment_id = _create_payment(webhook_engine, "chatdart_stale_hmac")
    raw_body = _raw_webhook_body(payment_id)
    secret, secret_bytes = _portone_secret()
    monkeypatch.setattr("app.api.v1.membership.settings.ENV", "production")
    monkeypatch.setattr("app.services.pg_service.settings.PG_PROVIDER", "portone")
    monkeypatch.setattr("app.services.pg_service.settings.PORTONE_WEBHOOK_SECRET", secret)

    headers = _portone_headers(raw_body, secret_bytes, timestamp=int(time.time()) - 601)
    resp = _post_raw_webhook(webhook_client, raw_body, headers)

    assert resp.status_code == 401
