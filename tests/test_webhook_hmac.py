import base64
import hashlib
import hmac
import time

from app.services import pg_service
from app.services.pg_service import verify_webhook_signature


def _portone_secret() -> tuple[str, bytes]:
    secret_bytes = b"portone-webhook-secret"
    return "whsec_" + base64.b64encode(secret_bytes).decode(), secret_bytes


def _portone_headers(raw_body: bytes, secret_bytes: bytes, timestamp: int | None = None) -> dict[str, str]:
    timestamp_value = str(timestamp or int(time.time()))
    webhook_id = "webhook-test-id"
    message = f"{webhook_id}.{timestamp_value}.".encode() + raw_body
    signature = base64.b64encode(hmac.new(secret_bytes, message, hashlib.sha256).digest()).decode()
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp_value,
        "webhook-signature": f"v1,{signature}",
    }


def _toss_headers(raw_body: bytes, secret: str, timestamp: int | None = None) -> dict[str, str]:
    timestamp_value = str(timestamp or int(time.time()))
    message = raw_body + f":{timestamp_value}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {
        "tosspayments-webhook-signature": signature,
        "tosspayments-webhook-transmission-time": timestamp_value,
    }


def test_portone_valid_signature_passes(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret, secret_bytes = _portone_secret()
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "portone")
    monkeypatch.setattr(pg_service.settings, "PORTONE_WEBHOOK_SECRET", secret)

    assert verify_webhook_signature(raw_body, _portone_headers(raw_body, secret_bytes)) is True


def test_portone_invalid_signature_fails(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret, _ = _portone_secret()
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "portone")
    monkeypatch.setattr(pg_service.settings, "PORTONE_WEBHOOK_SECRET", secret)

    headers = _portone_headers(raw_body, b"wrong-secret")
    assert verify_webhook_signature(raw_body, headers) is False


def test_portone_stale_timestamp_fails(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret, secret_bytes = _portone_secret()
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "portone")
    monkeypatch.setattr(pg_service.settings, "PORTONE_WEBHOOK_SECRET", secret)

    headers = _portone_headers(raw_body, secret_bytes, timestamp=int(time.time()) - 601)
    assert verify_webhook_signature(raw_body, headers) is False


def test_portone_missing_headers_fails(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret, _ = _portone_secret()
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "portone")
    monkeypatch.setattr(pg_service.settings, "PORTONE_WEBHOOK_SECRET", secret)

    assert verify_webhook_signature(raw_body, {"webhook-id": "id"}) is False


def test_portone_multiple_signatures_accepts_any_match(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret, secret_bytes = _portone_secret()
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "portone")
    monkeypatch.setattr(pg_service.settings, "PORTONE_WEBHOOK_SECRET", secret)

    headers = _portone_headers(raw_body, secret_bytes)
    headers["webhook-signature"] = "v1,bad " + headers["webhook-signature"]
    assert verify_webhook_signature(raw_body, headers) is True


def test_toss_valid_signature_passes(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret = "toss-webhook-secret"
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "toss")
    monkeypatch.setattr(pg_service.settings, "TOSS_WEBHOOK_SECRET_KEY", secret)

    assert verify_webhook_signature(raw_body, _toss_headers(raw_body, secret)) is True


def test_toss_invalid_signature_fails(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "toss")
    monkeypatch.setattr(pg_service.settings, "TOSS_WEBHOOK_SECRET_KEY", "toss-webhook-secret")

    headers = _toss_headers(raw_body, "wrong-secret")
    assert verify_webhook_signature(raw_body, headers) is False


def test_toss_stale_timestamp_fails(monkeypatch):
    raw_body = b'{"payment_id":"p1"}'
    secret = "toss-webhook-secret"
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "toss")
    monkeypatch.setattr(pg_service.settings, "TOSS_WEBHOOK_SECRET_KEY", secret)

    headers = _toss_headers(raw_body, secret, timestamp=int(time.time()) - 601)
    assert verify_webhook_signature(raw_body, headers) is False


def test_unsupported_pg_provider_returns_false(monkeypatch):
    monkeypatch.setattr(pg_service.settings, "PG_PROVIDER", "unknown")
    assert verify_webhook_signature(b"{}", {}) is False
