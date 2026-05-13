"""Pytest fixtures + per-test env hygiene."""

import os
import pytest

from src.catalog import reset_catalog_cache
from src.config import reset_settings_cache

# Env-var prefixes/names that any test could accidentally inherit from the
# developer's shell. We scrub them before every test so behavior is
# reproducible regardless of who runs it.
_PROTECTED_NAMES = (
    "OPENMETADATA_PASSWORD",
    "DB_PASSWORD",
    "GEMINI_API_KEY",
    "RAG_API_KEY",
    "EUNOMIA_CONFIG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any EUNOMIA_*/well-known secret env vars before each test."""
    for k in list(os.environ):
        if k.startswith("EUNOMIA_") or k in _PROTECTED_NAMES:
            monkeypatch.delenv(k, raising=False)
    reset_settings_cache()
    reset_catalog_cache()
    yield
    reset_settings_cache()
    reset_catalog_cache()
