"""logging_setup: handler attachment, level routing, idempotency, file output."""

import logging
import logging.handlers

import pytest

from src.config import load_settings, reset_settings_cache
from src.logging_setup import _HANDLER_TAG, configure_logging


def _eunomia_handler_count(root: logging.Logger) -> int:
    return sum(1 for h in root.handlers if getattr(h, _HANDLER_TAG, False))


def test_default_attaches_console_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    root = logging.getLogger()
    assert _eunomia_handler_count(root) == 2
    # File handler points at <tmp_path>/eunomia.log
    file_handler = next(
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert str(tmp_path) in file_handler.baseFilename


def test_console_disabled_drops_stream_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EUNOMIA_LOGGING__CONSOLE", "false")
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    root = logging.getLogger()
    # No StreamHandler (apart from possibly the RotatingFileHandler which is a subclass)
    consoles = [
        h for h in root.handlers
        if getattr(h, _HANDLER_TAG, False)
        and isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert consoles == []


def test_file_disabled_drops_rotating_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EUNOMIA_LOGGING__FILE", "false")
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    root = logging.getLogger()
    rotators = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotators == []


def test_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    configure_logging(s)
    configure_logging(s)
    root = logging.getLogger()
    assert _eunomia_handler_count(root) == 2


@pytest.mark.parametrize("level,expected", [
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARN", logging.WARNING),
    ("ERROR", logging.ERROR),
])
def test_levels_map_to_stdlib(tmp_path, monkeypatch, level, expected):
    monkeypatch.setenv("EUNOMIA_LOGGING__LEVEL", level)
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    assert logging.getLogger().level == expected


def test_messages_written_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EUNOMIA_LOGGING__LEVEL", "INFO")
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    log = logging.getLogger("eunomia.test_messages_written_to_file")
    log.info("hello-from-test")
    # Force flush
    for h in logging.getLogger().handlers:
        h.flush()
    log_file = tmp_path / "eunomia.log"
    assert log_file.exists()
    contents = log_file.read_text()
    assert "hello-from-test" in contents
    assert "INFO" in contents


def test_debug_suppressed_at_info(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EUNOMIA_LOGGING__LEVEL", "INFO")
    reset_settings_cache()
    s = load_settings()
    configure_logging(s)
    log = logging.getLogger("eunomia.test_debug_suppressed_at_info")
    log.debug("DEBUG_LINE_SHOULD_NOT_APPEAR")
    log.info("INFO_LINE_SHOULD_APPEAR")
    for h in logging.getLogger().handlers:
        h.flush()
    contents = (tmp_path / "eunomia.log").read_text()
    assert "DEBUG_LINE_SHOULD_NOT_APPEAR" not in contents
    assert "INFO_LINE_SHOULD_APPEAR" in contents
