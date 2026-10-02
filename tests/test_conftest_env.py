import threading

from app.core.config import settings


def test_background_prewarm_disabled_by_default():
    assert settings.ENABLE_BACKGROUND_PREWARM is False


def test_client_does_not_start_daily_prewarm_thread(client):
    assert "daily-prewarm" not in {thread.name for thread in threading.enumerate()}
