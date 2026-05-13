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
from typing import Dict, List, Optional, Tuple

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
    """Authority-side catalog client. Two responsibility surfaces:

    1. (Phase A-C / legacy) `get_allowed_views_and_pii(user_identity)`
       — returns the FULL set of views a user may see PLUS PII tag info,
         in a single call. Used when authorization decisions live in the
         catalog client itself (mock with hardcoded role map).

    2. (Phase D) `fetch_views_with_pii(view_names)`
       — given a list of view names already resolved elsewhere (e.g. via
         OM /search/query against tag policies), fetch the rich view info
         and PII tag info. Authorization happened BEFORE this call.

    Both must exist on every CatalogClient subclass so the route layer
    can pick the right path based on whether OM is the policy decision
    point (Phase D) or the catalog client is (Phase A-C mock).

    The SQL validator always validates against the catalog's allowed-view
    list — never the RAG-narrowed list — for defense in depth.
    """

    @abstractmethod
    async def get_allowed_views_and_pii(
        self, user_identity: dict
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Phase A-C: returns (allowed_views, pii_tag_map) for the given user.

        Args:
            user_identity: legacy shape with at minimum `{"role": str}`.

        Returns:
            allowed_views: list of ViewInfo the user may query (may be empty).
            pii_tag_map:   {view_name: [column_name, ...]} for PII columns.
        """
        ...

    @abstractmethod
    async def fetch_views_with_pii(
        self,
        view_names: List[str],
        bearer_token: Optional[str] = None,
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Phase D: hydrate a pre-authorized list of view names.

        Authorization is assumed to have happened upstream (typically via
        ``src.catalog.om_access.resolve_allowed_views`` which queries OM's
        tag-policy index). This call only fetches the rich payload + PII tag
        info for those views.

        Args:
            view_names:   list of bare view names (no schema prefix).
            bearer_token: user's JWT to forward to OpenMetadata. When OM is
                          in OIDC-only mode, no admin login is available, so
                          the user's token is the only way to authenticate.
                          Mock implementations ignore this parameter.

        Returns:
            views:       ViewInfo objects in the same order as ``view_names``,
                         skipping any that don't exist in the catalog.
            pii_tags:    {view_name: [pii_column_name, ...]} — only views with
                         at least one PII-tagged column appear in the map.
        """
        ...
