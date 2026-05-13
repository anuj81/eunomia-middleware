"""ComposedCatalog — wraps a CatalogClient + optional RagClient.

This is the single object the route layer talks to. It enforces the two-stage
discovery model AND branches between the Phase A-C legacy path and the
Phase D real-Keycloak path based on ``settings.openmetadata.mock``.

Legacy (Phase A-C) path  (settings.openmetadata.mock = true):
    1. CatalogClient.get_allowed_views_and_pii(user_identity)
         — uses the in-memory role→view map; identity carries `role` (singular).
    2. Optional RAG narrowing for the LLM prompt.

Phase D path  (settings.openmetadata.mock = false):
    1. om_access.resolve_allowed_views(identity, settings)
         — OM is the policy decision point (tag policies via /search/query).
    2. CatalogClient.fetch_views_with_pii(view_names)
         — hydrate full ViewInfo + PII tag info for those views.
    3. Optional RAG narrowing for the LLM prompt.

Either way the route receives the same CatalogDiscovery shape:
    allowed_views      : full list for the SQL validator (defense in depth)
    relevant_views     : RAG-narrowed subset for the LLM prompt
    pii_tags           : OM-authoritative PII map for the masker
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import Settings
from .base import CatalogClient, PiiTagMap, ViewInfo
from .om_access import AccessDenied, OMAccessError, resolve_allowed_views
from .rag_base import RagClient, RagUnavailable

logger = logging.getLogger(__name__)


@dataclass
class CatalogDiscovery:
    allowed_views: List[ViewInfo]
    relevant_views: List[ViewInfo]
    pii_tags: PiiTagMap


class ComposedCatalog:
    def __init__(
        self,
        catalog: CatalogClient,
        rag: Optional[RagClient],
        top_k: int,
        settings: Optional[Settings] = None,
    ):
        self._catalog = catalog
        self._rag = rag
        self._top_k = top_k
        # Settings is optional so existing tests that construct ComposedCatalog
        # directly without a Settings still work. When unset, we default to the
        # legacy path (which is the Phase A-C behavior).
        self._settings = settings

    @property
    def rag_enabled(self) -> bool:
        return self._rag is not None

    async def discover(
        self,
        nlq_query: str,
        user_identity: dict,
    ) -> CatalogDiscovery:
        """Run discovery + optional relevance narrowing."""
        # Pick the path. Default to legacy if no settings (test-construction).
        use_phase_d = bool(self._settings) and not self._settings.openmetadata.mock

        try:
            if use_phase_d:
                allowed_views, pii_tags = await self._discover_phase_d(user_identity)
            else:
                allowed_views, pii_tags = await self._discover_legacy(user_identity)
        except AccessDenied:
            # User has no Eunomia access roles. Return empty discovery.
            logger.info(
                "AccessDenied for user=%s — no Eunomia access roles",
                user_identity.get("preferred_username")
                or user_identity.get("sub")
                or user_identity.get("role"),
            )
            return CatalogDiscovery(allowed_views=[], relevant_views=[], pii_tags={})

        # Apply optional RAG narrowing.
        relevant_views = await self._narrow_via_rag(nlq_query, allowed_views)

        logger.info(
            "Catalog discovery (%s): user=%s allowed=%d relevant=%d pii_views=%d",
            "phase_d" if use_phase_d else "legacy",
            user_identity.get("preferred_username")
            or user_identity.get("sub")
            or user_identity.get("role"),
            len(allowed_views), len(relevant_views), len(pii_tags),
        )
        return CatalogDiscovery(
            allowed_views=allowed_views,
            relevant_views=relevant_views,
            pii_tags=pii_tags,
        )

    # --------------------------------------------------------------- paths #

    async def _discover_legacy(self, identity: dict):
        return await self._catalog.get_allowed_views_and_pii(identity)

    async def _discover_phase_d(self, identity: dict):
        # 1. Resolve allowed view names by hitting OM /search/query.
        names = resolve_allowed_views(identity, self._settings)  # may raise AccessDenied / OMAccessError
        # 2. Hydrate ViewInfo + PII tags for those names. We forward the user's
        #    JWT so OM accepts the call (OM is in OIDC-only mode and rejects
        #    admin-password login when configured for custom-oidc).
        return await self._catalog.fetch_views_with_pii(
            names, bearer_token=identity.get("raw_token"),
        )

    # --------------------------------------------------------- narrowing #

    async def _narrow_via_rag(
        self,
        nlq_query: str,
        allowed_views: List[ViewInfo],
    ) -> List[ViewInfo]:
        if self._rag is None or not allowed_views:
            return allowed_views

        # Latency optimization (no security implication): if the allowed set
        # already fits in the context budget, skip the RAG hop.
        if len(allowed_views) <= self._top_k:
            logger.debug(
                "Allowed set size %d ≤ top_k %d — skipping RAG narrowing.",
                len(allowed_views), self._top_k,
            )
            return allowed_views

        try:
            ranked = await self._rag.retrieve(nlq_query, allowed_views, self._top_k)
        except RagUnavailable as e:
            logger.warning(
                "RAG unavailable (%s) — falling back to full allowed list "
                "(LLM context will include all %d views).",
                e, len(allowed_views),
            )
            return allowed_views

        # Defense in depth: enforce subset-of-allowed even if RagClient misbehaved.
        allowed_names = {v.name for v in allowed_views}
        clean_ranked = [v for v in ranked if v.name in allowed_names]
        if len(clean_ranked) != len(ranked):
            logger.error(
                "RagClient returned views outside the allowed set — dropped: %s",
                [v.name for v in ranked if v.name not in allowed_names],
            )
        return clean_ranked
