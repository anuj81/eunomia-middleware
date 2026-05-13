"""Catalog client abstractions.

A CatalogClient answers a single question:

    "Given a user identity, which physical views are they allowed to see,
     and which of those columns carry PII?"

Concrete implementations:
    - OpenMetadataClient      — real OM REST integration
    - MockOpenMetadataClient  — in-memory fixture, no network

The route layer never imports either subclass directly; it goes through
`src.catalog.get_catalog_client(settings)` which is the factory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# DTOs                                                                        #
# --------------------------------------------------------------------------- #


class ColumnInfo(BaseModel):
    """A single column on a view.

    `description` is best-effort — OM may not have one, the LLM prompt should
    handle missing descriptions gracefully.
    """

    name: str
    description: str = ""


class ViewInfo(BaseModel):
    """A physical view exposed to the user."""

    name: str
    description: str = ""
    columns: List[ColumnInfo] = Field(default_factory=list)

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]


# pii_tags shape: {view_name: [pii_column_name, ...]}
PiiTagMap = Dict[str, List[str]]


# --------------------------------------------------------------------------- #
# Abstract base                                                               #
# --------------------------------------------------------------------------- #


class CatalogClient(ABC):
    """Authority-side catalog: returns the FULL set of views a user may see.

    Note: this layer does NOT take the NLQ query. Relevance narrowing is the
    job of `ComposedCatalog` (Phase C), which sits on top of a CatalogClient
    and an optional RagClient. The split is intentional:

        - CatalogClient = authorization (whitelist of views the user may touch)
        - RagClient     = relevance (top-K of that whitelist for the prompt)

    The SQL validator always validates against the CatalogClient's full set —
    never the RAG-narrowed list — for defense in depth.
    """

    @abstractmethod
    async def get_allowed_views_and_pii(
        self, user_identity: dict
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Return (allowed_views, pii_tag_map) for the given user.

        Both come from the same authority (OpenMetadata in the real client,
        an in-memory fixture in the mock).

        Args:
            user_identity: At minimum has {"role": str}. The auth layer may
                attach additional claims (domain, sub, ...) which subclasses
                are free to use.

        Returns:
            allowed_views: list of ViewInfo the user may query (may be empty).
            pii_tag_map:   {view_name: [column_name, ...]} for columns the
                           middleware must mask in results.
        """
        ...
