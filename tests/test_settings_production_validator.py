import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _valid_settings_kwargs(**overrides):
    values = {
        "ENV": "production",
        "DART_API_KEY": "dart",
        "API_SECRET_KEY": "api-secret",
        "JWT_SECRET_KEY": "jwt-secret",
        "SENTRY_DSN": "https://example@sentry.io/1",
        "PG_PROVIDER": "portone",
        "PORTONE_API_KEY": "portone-key",
        "PORTONE_API_SECRET": "portone-secret",
        "PORTONE_WEBHOOK_SECRET": "whsec_dGVzdA==",
        "TOSS_SECRET_KEY": "",
        "TOSS_WEBHOOK_SECRET_KEY": "",
        "REQUIRE_EMAIL_VERIFICATION": False,
        "MAIL_USERNAME": "",
        "MAIL_PASSWORD": "",
    }
    values.update(overrides)
    return values


def test_production_requires_sentry_dsn():
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_valid_settings_kwargs(SENTRY_DSN=""))

    assert "SENTRY_DSN" in str(exc_info.value)


def test_staging_requires_sentry_dsn():
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_valid_settings_kwargs(ENV="staging", SENTRY_DSN=""))

    assert "SENTRY_DSN" in str(exc_info.value)


def test_development_allows_empty_sentry_dsn():
    settings = Settings(**_valid_settings_kwargs(
        ENV="development",
        SENTRY_DSN="",
        PORTONE_API_KEY="",
        PORTONE_API_SECRET="",
        PORTONE_WEBHOOK_SECRET="",
    ))

    assert settings.SENTRY_DSN == ""


def test_production_allows_complete_secrets():
    settings = Settings(**_valid_settings_kwargs())

    assert settings.ENV == "production"


def test_email_verification_requires_mail_username():
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_valid_settings_kwargs(
            REQUIRE_EMAIL_VERIFICATION=True,
            MAIL_USERNAME="",
            MAIL_PASSWORD="password",
        ))

    assert "MAIL_USERNAME" in str(exc_info.value)


def test_email_verification_requires_mail_password():
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_valid_settings_kwargs(
            REQUIRE_EMAIL_VERIFICATION=True,
            MAIL_USERNAME="user",
            MAIL_PASSWORD="",
        ))

    assert "MAIL_PASSWORD" in str(exc_info.value)


def test_email_verification_allows_complete_mail_credentials():
    settings = Settings(**_valid_settings_kwargs(
        REQUIRE_EMAIL_VERIFICATION=True,
        MAIL_USERNAME="user",
        MAIL_PASSWORD="password",
    ))

    assert settings.REQUIRE_EMAIL_VERIFICATION is True


def test_existing_portone_payment_secret_branch_still_reports_webhook_secret():
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_valid_settings_kwargs(PORTONE_WEBHOOK_SECRET=""))

    assert "PORTONE_WEBHOOK_SECRET" in str(exc_info.value)
