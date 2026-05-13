"""Phase-D access resolver: derive a user's allowed view names from their JWT roles.

The flow:

    JWT roles → access tags (via settings.authz.role_to_access_tag)
              → OM /search/query?q=tags.tagFQN:("tag1" OR "tag2")
              → list of view names

OM is the policy decision point for "which views does role X actually see".
The middleware never decides that — it only translates the user's JWT roles
into the tag-FQN search query.

Two modes:
    • Wildcard / admin: if any role maps to "*", skip the tag filter and
      return ALL tables in the configured schema.
    • Per-role tags: build an OR-of-tag-FQNs query against the search index.

Result is a list of bare view names (`finance_daily_revenue_view`, ...) —
no schema prefix. The middleware uses these to scope the LLM prompt and
the SQL validator's allow-list.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)


class OMAccessError(Exception):
    """Raised when the OM search call fails outright (network/auth/server error)."""


class AccessDenied(Exception):
    """Raised when a user has no Eunomia access roles and therefore no allowed views."""


# --------------------------------------------------------------------------- #
# Pure helpers (testable without an OM)                                       #
# --------------------------------------------------------------------------- #


def map_roles_to_tags(roles: List[str], settings: Settings) -> Set[str]:
    """Translate Keycloak realm roles into OM tag FQNs.

    Returns a SET of tag FQNs. The special string "*" denotes wildcard
    (admin bypass) — if "*" is in the result, treat as "see all".
    """
    role_map = settings.authz.role_to_access_tag
    tags: Set[str] = set()
    for r in roles:
        tag = role_map.get(r)
        if tag:
            tags.add(tag)
    return tags


def has_admin_bypass(roles: List[str], settings: Settings) -> bool:
    return settings.auth.admin_bypass_role in roles


def has_pii_unmask(roles: List[str], settings: Settings) -> bool:
    return settings.auth.unmask_pii_role in roles


def build_search_query(tags: Set[str]) -> str:
    """Build the q= value for OM's /search/query.

    For a single tag: `tags.tagFQN:"tag"`.
    For multiple:    `tags.tagFQN:("tag1" OR "tag2")`.
    Empty set:       `q=*` (caller should special-case this — usually means
                      "no access").
    """
    if not tags:
        return "*"
    quoted = [f'"{t}"' for t in sorted(tags)]
    if len(quoted) == 1:
        return f'tags.tagFQN:{quoted[0]}'
    return f'tags.tagFQN:({" OR ".join(quoted)})'


# --------------------------------------------------------------------------- #
# OM call                                                                     #
# --------------------------------------------------------------------------- #


def _search_om_tables(
    om_base_url: str,
    raw_token: str,
    q: str,
    limit: int = 100,
    timeout_seconds: float = 5.0,
) -> List[Dict[str, Any]]:
    """Call OM /api/v1/search/query with the given Lucene q= and return hits."""
    url = (
        om_base_url.rstrip("/")
        + f"/search/query?q={quote(q)}&index=table_search_index&from=0&size={limit}"
    )
    headers = {"Authorization": f"Bearer {raw_token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_seconds)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.exception("OM search failed: %s", e)
        raise OMAccessError(f"OM search failed: {e}") from e
    body = resp.json()
    hits = (body.get("hits") or {}).get("hits") or []
    return [h.get("_source") or {} for h in hits]


def resolve_allowed_views(
    identity: Dict[str, Any],
    settings: Settings,
) -> List[str]:
    """End-to-end: turn an identity into a sorted, deduped list of view names.

    Raises ``AccessDenied`` if the user has no view-granting roles at all.
    Raises ``OMAccessError`` on transport failure.
    """
    roles: List[str] = identity.get("roles") or []
    raw_token: str = identity.get("raw_token") or ""

    if has_admin_bypass(roles, settings):
        q = "*"
        logger.info("resolve_allowed_views: admin bypass — fetching all views.")
    else:
        tags = map_roles_to_tags(roles, settings)
        # Drop the wildcard sentinel if it sneaked in via a non-admin role
        # (it shouldn't, but be defensive).
        tags.discard("*")
        if not tags:
            logger.info(
                "resolve_allowed_views: identity has no Eunomia access roles (roles=%s)",
                roles,
            )
            raise AccessDenied(
                "User has no roles mapped to view-access tags."
            )
        q = build_search_query(tags)
        logger.debug("resolve_allowed_views: tags=%s  q=%s", sorted(tags), q)

    hits = _search_om_tables(
        om_base_url=settings.openmetadata.url,
        raw_token=raw_token,
        q=q,
    )
    names = sorted({h["name"] for h in hits if h.get("name")})
    logger.info(
        "resolve_allowed_views: user=%s roles=%s → %d views: %s",
        identity.get("preferred_username") or identity.get("sub"),
        roles, len(names), names,
    )
    return names
