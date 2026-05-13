"""ComposedCatalog — wraps an OM CatalogClient + optional RagClient.

This is the single object the route layer talks to in Phase C. It enforces
the two-stage discovery model:

    1. OpenMetadata returns the FULL authorization list (allowed_views, pii_tags).
       — This is the source of truth for everything downstream.

    2. Optionally, RAG narrows that list to the top-K most relevant views for
       the natural-language query.
       — RAG is purely a *ranking signal*. It can never widen authorization.

The route consumes both lists:

    - LLM prompt context  uses  CatalogDiscovery.relevant_views     (top-K)
    - SQL validator        uses  CatalogDiscovery.allowed_views      (full)
    - PII masker           uses  CatalogDiscovery.pii_tags

The validator-on-full-list rule is the *defense-in-depth* invariant. If the
LLM picks a view that IS authorized by OM but didn't happen to make the
RAG top-K (cheap to imagine when k is small), validation must accept it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .base import CatalogClient, PiiTagMap, ViewInfo
from .rag_base import RagClient, RagUnavailable

logger = logging.getLogger(__name__)


@dataclass
class CatalogDiscovery:
    """The result of ComposedCatalog.discover()."""

    allowed_views: List[ViewInfo]
    relevant_views: List[ViewInfo]   # subset of allowed_views, ordered by relevance
    pii_tags: PiiTagMap


class ComposedCatalog:
    def __init__(
        self,
        catalog: CatalogClient,
        rag: Optional[RagClient],
        top_k: int,
    ):
        self._catalog = catalog
        self._rag = rag
        self._top_k = top_k

    @property
    def rag_enabled(self) -> bool:
        return self._rag is not None

    async def discover(
        self,
        nlq_query: str,
        user_identity: dict,
    ) -> CatalogDiscovery:
        """Run the two-stage discovery: OM authorization → optional RAG ranking."""
        allowed_views, pii_tags = await self._catalog.get_allowed_views_and_pii(
            user_identity
        )

        # No RAG, or nothing to rank → relevant == allowed.
        if self._rag is None or not allowed_views:
            return CatalogDiscovery(
                allowed_views=allowed_views,
                relevant_views=allowed_views,
                pii_tags=pii_tags,
            )

        # If the allowed set already fits comfortably, skip the RAG hop —
        # RAG is for narrowing context windows, not for re-ordering tiny sets.
        # (This guard is mostly a latency optimization; the security model
        # doesn't depend on it.)
        if len(allowed_views) <= self._top_k:
            logger.debug(
                "Allowed set size %d ≤ top_k %d — skipping RAG narrowing.",
                len(allowed_views), self._top_k,
            )
            return CatalogDiscovery(
                allowed_views=allowed_views,
                relevant_views=allowed_views,
                pii_tags=pii_tags,
            )

        try:
            ranked = await self._rag.retrieve(
                nlq_query, allowed_views, self._top_k,
            )
        except RagUnavailable as e:
            logger.warning(
                "RAG unavailable (%s) — falling back to full allowed list "
                "(LLM context will include all %d views).",
                e, len(allowed_views),
            )
            ranked = allowed_views

        # Defense in depth: enforce subset-of-allowed even if RagClient misbehaved.
        allowed_names = {v.name for v in allowed_views}
        clean_ranked = [v for v in ranked if v.name in allowed_names]
        if len(clean_ranked) != len(ranked):
            logger.error(
                "RagClient returned views outside the allowed set — dropped: %s",
                [v.name for v in ranked if v.name not in allowed_names],
            )

        logger.info(
            "Catalog discovery: role=%r allowed=%d relevant=%d pii_views=%d",
            user_identity.get("role"),
            len(allowed_views), len(clean_ranked), len(pii_tags),
        )
        return CatalogDiscovery(
            allowed_views=allowed_views,
            relevant_views=clean_ranked,
            pii_tags=pii_tags,
        )
