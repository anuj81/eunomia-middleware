"""Real OpenMetadata REST client implementing CatalogClient.

Configuration comes from settings.openmetadata (URL, username, password).
The password is env-only (OPENMETADATA_PASSWORD) — never in YAML.

For each user identity we look up a static role -> allowed-view-name mapping
(this mapping should ideally come FROM OM itself in a later iteration;
keeping it here matches the existing behaviour). Then we fetch table metadata
from OM and filter columns + PII tags accordingly.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Dict, List, Optional, Tuple

import requests

from ..config import Settings
from .base import CatalogClient, ColumnInfo, PiiTagMap, ViewInfo

logger = logging.getLogger(__name__)

# Static role -> allowed views mapping.
# TODO: source this from OpenMetadata policies/teams directly.
_ROLE_VIEW_MAP: Dict[str, List[str]] = {
    "Finance User": [
        "finance_daily_revenue_view",
        "finance_customer_payment_history_view",
    ],
    "External Auditor": [
        "finance_customer_payment_history_view",
    ],
    "Marketing Lead": [
        "marketing_customer_ltv_view",
        "marketing_regional_performance_view",
    ],
    "Agency Partner": [
        "marketing_regional_performance_view",
    ],
}


class OpenMetadataClient(CatalogClient):
    """Talks to a running OpenMetadata instance over REST."""

    # Hardcoded for now — pull into settings.openmetadata.database_fqn if needed.
    _TABLES_DATABASE_FQN = (
        "zenith_mysql.zenith_corp_eunomia.zenith_corp_eunomia"
    )

    def __init__(self, settings: Settings):
        cfg = settings.openmetadata
        self._url = cfg.url.rstrip("/")
        self._username = cfg.username
        self._password = cfg.password  # may be None at construction — login lazy
        self._token: Optional[str] = None

    # ------------------------------------------------------------------ auth #

    def _login(self) -> None:
        """Acquire a bearer token from OM. Idempotent."""
        if self._token is not None:
            return
        if not self._password:
            logger.warning(
                "OpenMetadata password is not set (OPENMETADATA_PASSWORD). "
                "Authenticated requests will likely fail."
            )
            return
        try:
            pwd_b64 = base64.b64encode(self._password.encode()).decode()
            resp = requests.post(
                f"{self._url}/users/login",
                json={"email": self._username, "password": pwd_b64},
                timeout=10,
            )
            if resp.status_code == 200:
                self._token = resp.json().get("accessToken")
                logger.info("OpenMetadata login succeeded for %s", self._username)
            else:
                logger.error(
                    "OpenMetadata login failed: %s %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("OpenMetadata login raised")

    def _headers(self) -> Dict[str, str]:
        self._login()
        if not self._token:
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------- public #

    async def get_allowed_views_and_pii(
        self, user_identity: dict
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Phase A-C: returns views using the hardcoded `_ROLE_VIEW_MAP`.

        Phase D NOTE: this method is preserved for offline / mock paths but
        the real-Keycloak request flow uses ``om_access.resolve_allowed_views``
        + ``fetch_views_with_pii`` instead — the role→view decision happens
        in OM (via tag policies), not in this Python dict.
        """
        role = user_identity.get("role")
        allowed_names = _ROLE_VIEW_MAP.get(role or "", [])
        if not allowed_names:
            logger.info("No views allowed for role=%r", role)
            return [], {}

        tables = await asyncio.to_thread(self._fetch_tables)
        views, pii_tags = _filter_views(tables, allowed_names)
        logger.debug(
            "OM returned %d views for role=%r, pii_tagged_views=%d",
            len(views), role, len(pii_tags),
        )
        return views, pii_tags

    async def fetch_views_with_pii(
        self,
        view_names: List[str],
        bearer_token: Optional[str] = None,
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Phase D: hydrate ViewInfo + PII tags for a pre-authorized name list.

        Uses the user's JWT (``bearer_token``) when provided — required when
        OM is in OIDC-only mode (no admin login). Falls back to the
        admin login flow only when ``bearer_token`` is None (legacy / dev).
        """
        if not view_names:
            return [], {}

        # Decide which auth header to send.
        if bearer_token:
            req_headers = {
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            }
        else:
            req_headers = self._headers()

        def _fetch_one(name: str):
            fqn = f"{self._TABLES_DATABASE_FQN}.{name}"
            try:
                resp = requests.get(
                    f"{self._url}/tables/name/{fqn}",
                    params={"fields": "columns,tags"},
                    headers=req_headers,
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "OM fetch_views_with_pii: %s → HTTP %s", name, resp.status_code,
                    )
                    return None
                return resp.json()
            except Exception:
                logger.exception("OM fetch_views_with_pii failed for %s", name)
                return None

        # Sequential is fine for our scale; asyncio.to_thread keeps the loop free.
        raw_tables: List[Optional[dict]] = []
        for n in view_names:
            raw_tables.append(await asyncio.to_thread(_fetch_one, n))

        views: List[ViewInfo] = []
        pii_tags: PiiTagMap = {}
        for raw in raw_tables:
            if not raw:
                continue
            v, p = _extract_view_and_pii(raw)
            if v is None:
                continue
            views.append(v)
            if p:
                pii_tags[v.name] = p
        return views, pii_tags

    # ------------------------------------------------------------- internal #

    def _fetch_tables(self) -> List[dict]:
        try:
            resp = requests.get(
                f"{self._url}/tables",
                params={
                    "database": self._TABLES_DATABASE_FQN,
                    "fields": "columns,tags",
                    "limit": 100,
                },
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception:
            logger.exception("OpenMetadata table fetch failed")
            return []


# --------------------------------------------------------------------------- #
# Helpers (pure, testable)                                                    #
# --------------------------------------------------------------------------- #


def _extract_view_and_pii(raw: dict) -> Tuple[Optional[ViewInfo], List[str]]:
    """Convert one OM /tables payload into (ViewInfo, [pii_column_names])."""
    name = raw.get("name")
    if not name:
        return None, []
    columns: List[ColumnInfo] = []
    pii_cols: List[str] = []
    for c in raw.get("columns") or []:
        col_name = c.get("name")
        if not col_name:
            continue
        columns.append(
            ColumnInfo(name=col_name, description=c.get("description") or "")
        )
        for tag in c.get("tags") or []:
            if tag.get("tagFQN") == "PII.Sensitive":
                pii_cols.append(col_name)
    view = ViewInfo(
        name=name,
        description=raw.get("description") or "",
        columns=columns,
    )
    return view, pii_cols


def _filter_views(
    om_tables: List[dict], allowed_names: List[str]
) -> Tuple[List[ViewInfo], PiiTagMap]:
    allowed_set = set(allowed_names)
    views: List[ViewInfo] = []
    pii_tags: PiiTagMap = {}
    for t in om_tables:
        if t.get("name") not in allowed_set:
            continue
        view, pii_cols = _extract_view_and_pii(t)
        if view is None:
            continue
        views.append(view)
        if pii_cols:
            pii_tags[view.name] = pii_cols
    return views, pii_tags
