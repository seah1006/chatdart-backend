from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _auth_settings():
    settings = MagicMock()
    settings.API_SECRET_KEY = "pytest-test-api-key-12345"
    return settings


class _RecordingThread:
    names = []

    def __init__(self, target=None, daemon=None, name=None):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.names.append(name)

    def start(self):
        return None


def _reset_recorded_threads():
    _RecordingThread.names = []


def test_prewarm_threads_skipped_when_disabled():
    from app import main as main_module

    _reset_recorded_threads()
    with patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", False), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch("app.main.alembic_command.upgrade"), \
         patch("app.main.threading.Thread", _RecordingThread):
        with TestClient(main_module.app):
            pass

    assert "dart-init" in _RecordingThread.names
    assert "prewarm-induty" not in _RecordingThread.names
    assert "prewarm-krx-listed" not in _RecordingThread.names
    assert "prewarm-krx-listed" not in _RecordingThread.names
    assert "prewarm" not in _RecordingThread.names
    assert "prewarm-recommendation" not in _RecordingThread.names
    assert "daily-prewarm" not in _RecordingThread.names


def test_prewarm_threads_started_when_enabled():
    from app import main as main_module

    _reset_recorded_threads()
    with patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", True), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch("app.main.alembic_command.upgrade"), \
         patch("app.main.threading.Thread", _RecordingThread):
        with TestClient(main_module.app):
            pass

    assert "dart-init" in _RecordingThread.names
    assert "prewarm-induty" in _RecordingThread.names
    assert "prewarm-krx-listed" in _RecordingThread.names
    assert "prewarm-krx-listed" in _RecordingThread.names
    assert "prewarm" in _RecordingThread.names
    assert "prewarm-recommendation" in _RecordingThread.names
    assert "daily-prewarm" in _RecordingThread.names


def test_startup_continues_when_migration_fails_and_not_strict():
    from app import main as main_module

    with patch.object(main_module.settings, "STRICT_MIGRATION", False), \
         patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", False), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch("app.main.alembic_command.upgrade", side_effect=Exception("boom")):
        with TestClient(main_module.app) as client:
            response = client.get("/health")

    assert response.status_code == 200


def test_startup_raises_when_migration_fails_and_strict():
    from app import main as main_module

    with patch.object(main_module.settings, "STRICT_MIGRATION", True), \
         patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", False), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch("app.main.alembic_command.upgrade", side_effect=Exception("boom")), \
         pytest.raises(Exception, match="boom"):
        with TestClient(main_module.app):
            pass
