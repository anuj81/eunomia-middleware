"""Mock OpenMetadata client — no network, deterministic fixtures.

Mirrors `planning/schema_and_data_example.md` and the role definitions in
`planning/organization_structure_example.md`. Suitable for:
    - dev loops where you don't want to run OM
    - integration tests
    - CI

Fixtures here are intentionally hand-maintained (not pulled from OM) so
they double as living documentation of the expected catalog shape.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .base import CatalogClient, ColumnInfo, PiiTagMap, ViewInfo

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Fixture                                                                     #
# --------------------------------------------------------------------------- #
# View-name → ViewInfo. Names use underscore form (matching the real OM names).

_VIEW_FIXTURE: Dict[str, ViewInfo] = {
    "finance_daily_revenue_view": ViewInfo(
        name="finance_daily_revenue_view",
        description="Aggregated daily revenue. No PII.",
        columns=[
            ColumnInfo(name="day", description="UTC date of revenue rollup."),
            ColumnInfo(name="gross_revenue", description="Sum of order totals."),
            ColumnInfo(name="net_revenue", description="Gross minus refunds."),
            ColumnInfo(name="order_count", description="Number of distinct orders."),
        ],
    ),
    "finance_customer_payment_history_view": ViewInfo(
        name="finance_customer_payment_history_view",
        description="Detailed customer payment history. Inherits PII from dim_customers.",
        columns=[
            ColumnInfo(name="customer_id", description="Internal customer identifier."),
            ColumnInfo(name="first_name", description="Customer first name (PII)."),
            ColumnInfo(name="last_name", description="Customer last name (PII)."),
            ColumnInfo(name="email", description="Customer email (PII)."),
            ColumnInfo(name="payment_date", description="Timestamp of payment."),
            ColumnInfo(name="amount", description="Payment amount in USD."),
            ColumnInfo(name="card_last_four", description="Last four digits of card (PII)."),
        ],
    ),
    "marketing_customer_ltv_view": ViewInfo(
        name="marketing_customer_ltv_view",
        description="Customer lifetime value rollup. Contains email PII.",
        columns=[
            ColumnInfo(name="customer_id", description="Internal customer identifier."),
            ColumnInfo(name="email", description="Customer email (PII)."),
            ColumnInfo(name="ltv", description="Lifetime value in USD."),
            ColumnInfo(name="first_order_date", description="Date of first order."),
            ColumnInfo(name="last_order_date", description="Date of most recent order."),
        ],
    ),
    "marketing_regional_performance_view": ViewInfo(
        name="marketing_regional_performance_view",
        description="High-level regional performance metrics. No PII.",
        columns=[
            ColumnInfo(name="region", description="Geographic region."),
            ColumnInfo(name="total_orders", description="Order count per region."),
            ColumnInfo(name="total_revenue", description="Revenue per region (USD)."),
            ColumnInfo(name="active_customers", description="Distinct active customers."),
        ],
    ),
}

# PII columns per view — same source of truth the real OM would have via tags.
_PII_FIXTURE: PiiTagMap = {
    "finance_customer_payment_history_view": [
        "first_name", "last_name", "email", "card_last_four",
    ],
    "marketing_customer_ltv_view": ["email"],
}

# Role → allowed view names. Accepts BOTH the legacy "Finance User"-style
# strings (Phase A-C tests; current `role` field) and the Phase D
# `eunomia-X` role names (from the mock auth provider's identity claims).
# Phase D mapping mirrors `settings.authz.role_to_access_tag`.
_ROLE_VIEW_MAP: Dict[str, List[str]] = {
    # Legacy (Phase A-C) — keyed on the user_identity['role'] string.
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
    # Phase D — keyed on JWT realm role names.
    "eunomia-finance-user": [
        "finance_daily_revenue_view",
        "finance_customer_payment_history_view",
    ],
    "eunomia-external-auditor": [
        "finance_customer_payment_history_view",
    ],
    "eunomia-marketing-lead": [
        "marketing_customer_ltv_view",
        "marketing_regional_performance_view",
    ],
    "eunomia-agency-partner": [
        "marketing_regional_performance_view",
    ],
    "eunomia-om-admin": [
        "finance_daily_revenue_view",
        "finance_customer_payment_history_view",
        "marketing_customer_ltv_view",
        "marketing_regional_performance_view",
    ],
}


class MockOpenMetadataClient(CatalogClient):
    """In-memory catalog client. Returns deterministic fixtures."""

    async def get_allowed_views_and_pii(
        self, user_identity: dict
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Returns the union of views granted by any role on the identity.

        Accepts both identity shapes:
            • legacy:  {"role": "Finance User"}
            • Phase D: {"roles": ["eunomia-finance-user", ...]}
        Unknown role strings are silently ignored.
        """
        candidate_roles: List[str] = []
        if user_identity.get("role"):
            candidate_roles.append(user_identity["role"])
        candidate_roles.extend(user_identity.get("roles") or [])

        allowed_names: List[str] = []
        seen: set = set()
        for r in candidate_roles:
            for v in _ROLE_VIEW_MAP.get(r, []):
                if v not in seen:
                    seen.add(v)
                    allowed_names.append(v)

        views = [_VIEW_FIXTURE[n] for n in allowed_names if n in _VIEW_FIXTURE]
        pii_tags = {
            n: cols for n, cols in _PII_FIXTURE.items()
            if n in {v.name for v in views}
        }
        logger.debug(
            "MockOpenMetadataClient: roles=%s → %d views, pii_views=%d",
            candidate_roles, len(views), len(pii_tags),
        )
        return views, pii_tags

    async def fetch_views_with_pii(
        self,
        view_names: List[str],
        bearer_token: Optional[str] = None,  # accepted for interface parity; ignored
    ) -> Tuple[List[ViewInfo], PiiTagMap]:
        """Phase D path: hydrate a pre-authorized list of view names from fixture."""
        views: List[ViewInfo] = []
        pii_tags: PiiTagMap = {}
        for name in view_names:
            view = _VIEW_FIXTURE.get(name)
            if view is None:
                logger.debug("MockOpenMetadataClient: unknown view %r — skipping", name)
                continue
            views.append(view)
            if name in _PII_FIXTURE:
                pii_tags[name] = list(_PII_FIXTURE[name])
        logger.debug(
            "MockOpenMetadataClient.fetch_views_with_pii: %d/%d hit, %d with pii",
            len(views), len(view_names), len(pii_tags),
        )
        return views, pii_tags
