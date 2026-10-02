import asyncio
import hashlib
import logging
from unittest.mock import AsyncMock


TEST_PASSWORD = "Abcd1234!@"


def _register(client, email="pii-user@example.com"):
    return client.post("/api/v1/auth/register", json={
        "email": email,
        "password": TEST_PASSWORD,
        "nickname": "pii",
        "terms_agreed": True,
    })


def test_register_log_omits_email(client, caplog, monkeypatch):
    from app.api.v1 import auth

    monkeypatch.setattr(auth.settings, "MAIL_USERNAME", "")
    caplog.set_level(logging.INFO, logger="app.api.v1.auth")

    response = _register(client)

    assert response.status_code == 201
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "pii-user@example.com" not in messages
    assert "user_id=" in messages


def test_login_log_omits_email(client, caplog, monkeypatch):
    from app.api.v1 import auth

    monkeypatch.setattr(auth.settings, "MAIL_USERNAME", "")
    _register(client, email="login-pii@example.com")
    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.api.v1.auth")

    response = client.post("/api/v1/auth/login", json={
        "email": "login-pii@example.com",
        "password": TEST_PASSWORD,
    })

    assert response.status_code == 200
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "login-pii@example.com" not in messages
    assert "user_id=" in messages


def test_collect_email_failure_log_masks_email(caplog, monkeypatch):
    from app.api.v1 import finance

    async_send = AsyncMock(side_effect=RuntimeError("mail down"))
    monkeypatch.setattr(finance.DartService, "fetch_and_process_data", lambda stock_code: None)
    monkeypatch.setattr(finance.DartService, "get_company_name_by_stock_code", lambda stock_code: "Test Corp")
    monkeypatch.setattr("app.services.email_service.send_collect_complete", async_send)
    caplog.set_level(logging.WARNING, logger="app.api.v1.finance")

    asyncio.run(finance._collect_with_email("005930", "collect-pii@example.com"))

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "collect-pii@example.com" not in messages
    assert "email_hash=" in messages


def test_mask_email_for_log_is_stable_sha256_prefix():
    from app.core.security import mask_email_for_log

    expected = hashlib.sha256(b"test@example.com").hexdigest()[:8]

    assert mask_email_for_log("test@example.com") == expected
    assert mask_email_for_log("") == "<none>"
    assert mask_email_for_log(None) == "<none>"
