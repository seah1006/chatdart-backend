from unittest.mock import patch

import pytest

import app.services.ai_summary_service as ai


@pytest.fixture(autouse=True)
def _reset_client():
    ai._client = None
    yield
    ai._client = None


def test_get_client_raises_when_key_empty(monkeypatch):
    monkeypatch.setattr(ai.settings, "OPENAI_API_KEY", "")

    with pytest.raises(RuntimeError):
        ai._get_client()


def test_get_client_uses_settings_key(monkeypatch):
    monkeypatch.setattr(ai.settings, "OPENAI_API_KEY", "sk-test-123")

    with patch.object(ai, "OpenAI") as mock_openai:
        client = ai._get_client()

    mock_openai.assert_called_once()
    _, kwargs = mock_openai.call_args
    assert kwargs.get("api_key") == "sk-test-123"
    assert client is mock_openai.return_value
