import logging
from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError as SAOperationalError


def _auth_settings():
    settings = MagicMock()
    settings.API_SECRET_KEY = "pytest-test-api-key-12345"
    return settings


def _upgrade_config(database_url, configure_logger=None):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    if configure_logger is not None:
        config.attributes["configure_logger"] = configure_logger
    return config


@pytest.fixture(autouse=True)
def restore_logging_state():
    root = logging.getLogger()
    logger_states = {
        name: (logger.disabled, logger.level, logger.handlers[:], logger.propagate)
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root_level = root.level
    root_handlers = root.handlers[:]
    yield
    root.setLevel(root_level)
    root.handlers.clear()
    root.handlers.extend(root_handlers)
    for name, state in logger_states.items():
        logger = logging.getLogger(name)
        logger.disabled, logger.level, handlers, logger.propagate = state
        logger.handlers.clear()
        logger.handlers.extend(handlers)
    for name, logger in logging.root.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and name not in logger_states:
            logger.disabled = False


def test_app_path_preserves_logging_during_upgrade(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.core.logging import setup_logging

    db_url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", db_url)
    setup_logging("INFO", use_json=True)
    handler = logging.getLogger().handlers[0]

    command.upgrade(_upgrade_config(db_url, configure_logger=False), "head")

    assert logging.getLogger("app.main").disabled is False
    assert logging.getLogger().level == logging.INFO
    assert handler in logging.getLogger().handlers


def test_cli_path_calls_file_config(tmp_path, monkeypatch):
    from app.core.config import settings

    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", db_url)
    with patch("logging.config.fileConfig") as mock_file_config:
        command.upgrade(_upgrade_config(db_url), "head")

    mock_file_config.assert_called_once_with("alembic.ini")


def test_app_path_skips_file_config(tmp_path, monkeypatch):
    from app.core.config import settings

    db_url = f"sqlite:///{tmp_path / 'skip.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", db_url)
    with patch("logging.config.fileConfig") as mock_file_config:
        command.upgrade(_upgrade_config(db_url, configure_logger=False), "head")

    mock_file_config.assert_not_called()


def test_lifespan_passes_configure_logger_false():
    from app import main as main_module

    with patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", False), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch("app.main.alembic_command.upgrade") as mock_upgrade:
        with TestClient(main_module.app):
            pass

    config = mock_upgrade.call_args.args[0]
    assert config.attributes["configure_logger"] is False


def test_lifespan_passes_configure_logger_false_to_stamp():
    from app import main as main_module

    with patch.object(main_module.settings, "ENABLE_BACKGROUND_PREWARM", False), \
         patch("app.services.dart_service.DartService.initialize"), \
         patch("app.services.dart_service.DartService._init_failed", True), \
         patch("app.dependencies.auth.settings", _auth_settings()), \
         patch(
             "app.main.alembic_command.upgrade",
             side_effect=SAOperationalError("table already exists", None, None),
         ), \
         patch("app.main.alembic_command.stamp") as mock_stamp:
        with TestClient(main_module.app):
            pass

    config = mock_stamp.call_args.args[0]
    assert config.attributes["configure_logger"] is False
