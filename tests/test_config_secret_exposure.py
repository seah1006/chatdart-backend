from pydantic import ValidationError
import pytest


@pytest.mark.parametrize(
    ("env_line", "error_type", "secret"),
    [
        ("NOT_DECLARED_SECRET=sk-TEST-UNDECLARED-VALUE", "extra_forbidden", "sk-TEST-UNDECLARED-VALUE"),
        ("AI_DAILY_LIMIT=not-an-int-sk-TEST-DECLARED-VALUE", "int_parsing", "sk-TEST-DECLARED-VALUE"),
    ],
)
def test_settings_validation_hides_secret_input(tmp_path, monkeypatch, env_line, error_type, secret):
    from app.core.config import Settings

    env_file = tmp_path / ".env.test"
    env_file.write_text(env_line + "\n", encoding="utf-8")
    monkeypatch.setenv("DART_API_KEY", "d" * 32)
    monkeypatch.setenv("API_SECRET_KEY", "a" * 32)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 32)
    monkeypatch.delenv("AI_DAILY_LIMIT", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=env_file)

    assert any(error["type"] == error_type for error in exc_info.value.errors())
    assert secret not in str(exc_info.value)
