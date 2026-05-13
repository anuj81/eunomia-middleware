"""Catalog package — factories + public DTOs.

Phase C wires three layers:
    - CatalogClient        (OM authority — real or mock)
    - RagClient            (relevance — real HTTP or mock keyword overlap)
    - ComposedCatalog      (ties them together; the single route-side handle)

Routes should depend on get_composed_catalog (FastAPI Depends).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from ..config import Settings, get_settings
from .base import CatalogClient, ColumnInfo, PiiTagMap, ViewInfo
from .composed import CatalogDiscovery, ComposedCatalog
from .openmetadata import OpenMetadataClient
from .openmetadata_mock import MockOpenMetadataClient
from .rag_base import RagClient, RagUnavailable
from .rag_client import HttpRagClient
from .rag_mock import MockRagClient

logger = logging.getLogger(__name__)

__all__ = [
    # DTOs
    "CatalogClient", "ColumnInfo", "PiiTagMap", "ViewInfo",
    "CatalogDiscovery",
    # Catalog impls
    "OpenMetadataClient", "MockOpenMetadataClient",
    # RAG impls
    "RagClient", "RagUnavailable", "HttpRagClient", "MockRagClient",
    # Composed orchestrator
    "ComposedCatalog",
    # Factories
    "get_catalog_client", "get_rag_client", "get_composed_catalog",
    "fastapi_composed_catalog",
    "reset_catalog_cache",
]


# --------------------------------------------------------------------------- #
# CatalogClient factory                                                       #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _build_catalog_client(mock: bool) -> CatalogClient:
    settings = get_settings()
    if mock:
        logger.info("Catalog: MockOpenMetadataClient (openmetadata.mock=true)")
        return MockOpenMetadataClient()
    logger.info(
        "Catalog: OpenMetadataClient → %s (openmetadata.mock=false)",
        settings.openmetadata.url,
    )
    return OpenMetadataClient(settings)


def get_catalog_client(settings: Optional[Settings] = None) -> CatalogClient:
    s = settings or get_settings()
    return _build_catalog_client(s.openmetadata.mock)


# --------------------------------------------------------------------------- #
# RagClient factory                                                           #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _build_rag_client(enabled: bool, mock: bool) -> Optional[RagClient]:
    if not enabled:
        logger.info("RAG: disabled (rag.enabled=false)")
        return None
    settings = get_settings()
    if mock:
        logger.info("RAG: MockRagClient (rag.mock=true)")
        return MockRagClient()
    logger.info(
        "RAG: HttpRagClient → %s (rag.mock=false)",
        settings.rag.url,
    )
    return HttpRagClient(settings)


def get_rag_client(settings: Optional[Settings] = None) -> Optional[RagClient]:
    s = settings or get_settings()
    return _build_rag_client(s.rag.enabled, s.rag.mock)


# --------------------------------------------------------------------------- #
# ComposedCatalog factory                                                     #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _build_composed_catalog(
    om_mock: bool, rag_enabled: bool, rag_mock: bool, top_k: int,
) -> ComposedCatalog:
    settings = get_settings()
    catalog = _build_catalog_client(om_mock)
    rag = _build_rag_client(rag_enabled, rag_mock)
    # Pass settings so ComposedCatalog can pick the legacy vs Phase D path.
    return ComposedCatalog(catalog=catalog, rag=rag, top_k=top_k, settings=settings)


def get_composed_catalog(settings: Optional[Settings] = None) -> ComposedCatalog:
    """Programmatic accessor — used by tests and by the FastAPI dep below."""
    s = settings or get_settings()
    return _build_composed_catalog(
        s.openmetadata.mock, s.rag.enabled, s.rag.mock, s.rag.top_k,
    )


def fastapi_composed_catalog() -> ComposedCatalog:
    """Zero-arg FastAPI ``Depends`` target.

    Don't depend on ``get_composed_catalog`` directly from a route — FastAPI
    introspects the signature, sees ``settings: Optional[Settings]``, decides
    ``Settings`` is a Pydantic BaseModel, and tries to inject it from the
    request body. The body schema then morphs into the embedded form
    ``{"req": {...}, "settings": {...}}`` and every POST returns 422.

    This wrapper has no parameters, so FastAPI just calls it.
    """
    return get_composed_catalog()


# --------------------------------------------------------------------------- #
# Cache control (used by tests)                                               #
# --------------------------------------------------------------------------- #


def reset_catalog_cache() -> None:
    _build_catalog_client.cache_clear()
    _build_rag_client.cache_clear()
    _build_composed_catalog.cache_clear()


# Back-compat alias for callers from Phase A.
reset_catalog_client_cache = reset_catalog_cache
