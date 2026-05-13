"""Centralized logging configuration for Eunomia Middleware.

Call ``configure_logging(settings)`` once at startup. It is idempotent so
uvicorn auto-reload (which re-imports the app module) won't pile up handlers.

Handlers attached to the root logger:
    - StreamHandler (stdout)               if settings.logging.console
    - RotatingFileHandler (<log_dir>/...)  if settings.logging.file

Uvicorn's own loggers (``uvicorn``, ``uvicorn.access``, ``uvicorn.error``) are
re-parented to the root so framework logs land in the same destinations.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime circular with config package
    from .config import Settings


_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_TAG = "eunomia_handler"  # marker so we recognise our own handlers
_LOG_FILENAME = "eunomia.log"

# Map our exposed level strings to stdlib logging constants. We intentionally
# only expose this subset (plus DEBUG) — see config.LoggingConfig.
_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,   # stdlib calls this WARNING; we accept the short form
    "ERROR": logging.ERROR,
}


def _resolve_log_dir(raw: Path) -> Path:
    """Resolve `raw` against the middleware project root if it is relative."""
    p = Path(raw)
    if not p.is_absolute():
        project_root = Path(__file__).resolve().parent.parent  # .../eunomia-middleware
        p = project_root / p
    return p


def _drop_existing_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_TAG, False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def configure_logging(settings: "Settings") -> None:
    """Wire root logger and uvicorn loggers per ``settings.logging``.

    Safe to call repeatedly: previously installed handlers are removed first.
    """
    cfg = settings.logging
    level = _LEVEL_MAP[cfg.level]  # validated upstream by pydantic Literal
    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    _drop_existing_handlers(root)
    root.setLevel(level)

    if cfg.console:
        sh = logging.StreamHandler()  # stderr by default — fine for terminals
        sh.setFormatter(formatter)
        sh.setLevel(level)
        setattr(sh, _HANDLER_TAG, True)
        root.addHandler(sh)

    if cfg.file:
        log_dir = _resolve_log_dir(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            filename=log_dir / _LOG_FILENAME,
            maxBytes=cfg.rotation.max_bytes,
            backupCount=cfg.rotation.backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        fh.setLevel(level)
        setattr(fh, _HANDLER_TAG, True)
        root.addHandler(fh)

    # Re-parent uvicorn / framework loggers so they share our handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        _drop_existing_handlers(lg)
        lg.handlers = []      # also drop uvicorn's default handlers
        lg.propagate = True   # bubble up to root
        lg.setLevel(level)

    # Tame noisy third parties: HTTP libs at WARNING unless we're in DEBUG.
    if level > logging.DEBUG:
        for name in ("httpx", "httpcore", "urllib3", "requests"):
            logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug(
        "logging configured: level=%s console=%s file=%s log_dir=%s",
        cfg.level, cfg.console, cfg.file,
        _resolve_log_dir(cfg.log_dir) if cfg.file else "(disabled)",
    )
