"""Eunomia Middleware — FastAPI entrypoint.

Two launch modes are supported:
    1. python -m src.main                    (argparse CLI flags fire)
    2. uvicorn src.main:app                  (settings still load; no CLI flags)

CLI flags are translated into environment variables before uvicorn boots so
that uvicorn's auto-reload child process inherits the same configuration.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from fastapi import FastAPI

from .api.routes import router
from .config import get_settings, reset_settings_cache
from .logging_setup import configure_logging

# .env must load before the first settings access so env-only secrets resolve.
load_dotenv()

# Configure logging at import time so messages emitted during app construction
# (route registration, middleware setup) flow through the same handlers as
# request logs. Idempotent — safe under uvicorn auto-reload.
configure_logging(get_settings())

app = FastAPI(title="Eunomia Middleware")
app.include_router(router, prefix="/v1")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eunomia-middleware",
        description="Governance-First NLQ Wrapper.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (default: config/eunomia.yaml or $EUNOMIA_CONFIG).",
    )
    parser.add_argument(
        "--verbose",
        type=str,
        choices=list(_LOG_LEVELS),
        default=None,
        help="Log level (overrides logging.level).",
    )
    mock = parser.add_mutually_exclusive_group()
    mock.add_argument(
        "--openmetadata-mock",
        dest="openmetadata_mock",
        action="store_true",
        default=None,
        help="Force use of MockOpenMetadataClient (overrides openmetadata.mock).",
    )
    mock.add_argument(
        "--no-openmetadata-mock",
        dest="openmetadata_mock",
        action="store_false",
        default=None,
        help="Force use of real OpenMetadataClient.",
    )
    return parser.parse_args()


def _apply_cli_to_env(args: argparse.Namespace) -> None:
    """Translate CLI flags into env vars so reload-child processes inherit them.

    These map to the same EUNOMIA_<SECTION>__<FIELD> form that pydantic-settings
    reads natively, so the override flows through both the parent process and
    any forked workers.
    """
    if args.config:
        os.environ["EUNOMIA_CONFIG"] = args.config
    if args.verbose is not None:
        os.environ["EUNOMIA_LOGGING__LEVEL"] = args.verbose
    if args.openmetadata_mock is not None:
        os.environ["EUNOMIA_OPENMETADATA__MOCK"] = (
            "true" if args.openmetadata_mock else "false"
        )


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #


def main() -> None:
    args = _parse_cli()
    _apply_cli_to_env(args)
    reset_settings_cache()  # drop anything cached before flags applied
    settings = get_settings()
    configure_logging(settings)  # re-apply with CLI-aware level

    # Server config will be wired more fully in Task #5; for now use whatever
    # the loaded settings say (defaults match the previous hardcoded values).
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
    )


if __name__ == "__main__":
    main()
