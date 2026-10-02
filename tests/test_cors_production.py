from app.core.config import Settings
from app.main import _build_cors_kwargs


def test_cors_dev_allows_ngrok_origin():
    dev_settings = Settings(ENV="development", DART_API_KEY="test")
    kwargs = _build_cors_kwargs(dev_settings)

    assert "allow_origin_regex" in kwargs
    assert "ngrok-free" in kwargs["allow_origin_regex"]
    assert "http://localhost:3000" in kwargs["allow_origins"]


def test_cors_production_rejects_ngrok_origin():
    prod_settings = Settings(
        ENV="production",
        DART_API_KEY="test",
        FRONTEND_URL="https://chatdart.com",
        API_SECRET_KEY="x" * 64,
        JWT_SECRET_KEY="y" * 64,
        SENTRY_DSN="https://example@sentry.io/1",
        WEBHOOK_SECRET="test-webhook-secret",
        PORTONE_API_KEY="test-portone-key",
        PORTONE_API_SECRET="test-portone-secret",
        PORTONE_WEBHOOK_SECRET="whsec_dGVzdA==",
    )
    kwargs = _build_cors_kwargs(prod_settings)

    assert "allow_origin_regex" not in kwargs
    assert "http://localhost:3000" not in kwargs["allow_origins"]
    assert kwargs["allow_origins"] == ["https://chatdart.com"]


def test_cors_production_allows_frontend_url():
    prod_settings = Settings(
        ENV="production",
        DART_API_KEY="test",
        FRONTEND_URL="https://chatdart.com",
        API_SECRET_KEY="x" * 64,
        JWT_SECRET_KEY="y" * 64,
        SENTRY_DSN="https://example@sentry.io/1",
        WEBHOOK_SECRET="test-webhook-secret",
        PORTONE_API_KEY="test-portone-key",
        PORTONE_API_SECRET="test-portone-secret",
        PORTONE_WEBHOOK_SECRET="whsec_dGVzdA==",
    )
    kwargs = _build_cors_kwargs(prod_settings)

    assert kwargs["allow_origins"] == ["https://chatdart.com"]
    assert "allow_origin_regex" not in kwargs


def test_cors_extra_origins_appended_in_production():
    prod_settings = Settings(
        ENV="production",
        DART_API_KEY="test",
        FRONTEND_URL="https://chatdart.com",
        CORS_EXTRA_ORIGINS="http://118.36.70.42:3000",
        API_SECRET_KEY="x" * 64,
        JWT_SECRET_KEY="y" * 64,
        SENTRY_DSN="https://example@sentry.io/1",
        WEBHOOK_SECRET="test-webhook-secret",
        PORTONE_API_KEY="test-portone-key",
        PORTONE_API_SECRET="test-portone-secret",
        PORTONE_WEBHOOK_SECRET="whsec_dGVzdA==",
    )
    kwargs = _build_cors_kwargs(prod_settings)

    assert kwargs["allow_origins"] == ["https://chatdart.com", "http://118.36.70.42:3000"]
    assert "allow_origin_regex" not in kwargs


def test_cors_extra_origins_multiple_and_whitespace():
    prod_settings = Settings(
        ENV="production",
        DART_API_KEY="test",
        FRONTEND_URL="https://chatdart.com",
        CORS_EXTRA_ORIGINS="http://a.com , http://b.com,",
        API_SECRET_KEY="x" * 64,
        JWT_SECRET_KEY="y" * 64,
        SENTRY_DSN="https://example@sentry.io/1",
        WEBHOOK_SECRET="test-webhook-secret",
        PORTONE_API_KEY="test-portone-key",
        PORTONE_API_SECRET="test-portone-secret",
        PORTONE_WEBHOOK_SECRET="whsec_dGVzdA==",
    )
    kwargs = _build_cors_kwargs(prod_settings)

    assert kwargs["allow_origins"] == ["https://chatdart.com", "http://a.com", "http://b.com"]


def test_cors_extra_origins_default_empty():
    dev_settings = Settings(ENV="development", DART_API_KEY="test", CORS_EXTRA_ORIGINS="")
    kwargs = _build_cors_kwargs(dev_settings)

    assert kwargs["allow_origins"] == [dev_settings.FRONTEND_URL, "http://localhost:3000"]
