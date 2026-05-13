"""ComposedCatalog Phase D path — when openmetadata.mock=false.

The legacy path is exercised by tests/test_composed_catalog.py. This file
covers the dispatch + Phase D behavior:
    - Phase D identity (with `roles[]` + `raw_token`) drives OM /search/query
    - PII tags come from CatalogClient.fetch_views_with_pii(names)
    - AccessDenied → empty CatalogDiscovery
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from src.catalog import (
    ComposedCatalog, MockOpenMetadataClient, MockRagClient,
)
from src.catalog import om_access
from src.catalog.base import ColumnInfo, ViewInfo
from src.config import load_settings, reset_settings_cache


# --------------------------------------------------------------------------- #
# fixture — Phase D settings (openmetadata.mock=false) + httpx mocking        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def phase_d_settings(monkeypatch):
    monkeypatch.setenv("EUNOMIA_OPENMETADATA__MOCK", "false")
    reset_settings_cache()
    return load_settings()


def _mock_om_search(monkeypatch, hits_by_query: Dict[str, List[Dict[str, Any]]]):
    """Patch httpx.get in om_access to return mock OM search hits."""
    def fake_get(url, headers=None, timeout=None):
        hits = []
        for needle, candidate_hits in hits_by_query.items():
            if needle in url:
                hits = candidate_hits
                break

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"hits": {"hits": [{"_source": h} for h in hits]}}
        return _R()
    monkeypatch.setattr(om_access.httpx, "get", fake_get)


def _identity_phase_d(roles: List[str], sub: str = "user-x") -> Dict[str, Any]:
    return {
        "sub": sub,
        "preferred_username": sub.replace(".", "_"),
        "email": f"{sub}@open-metadata.org",
        "roles": roles,
        "raw_token": "test-jwt",
        "provider": "keycloak",
    }


# --------------------------------------------------------------------------- #
# Phase D path                                                                #
# --------------------------------------------------------------------------- #


async def test_phase_d_finance_user_path(phase_d_settings, monkeypatch):
    """End-to-end: Phase D identity → OM search → fixture hydrate → discovery."""
    _mock_om_search(monkeypatch, {
        "eunomia-access.finance-user": [
            {"name": "finance_daily_revenue_view"},
            {"name": "finance_customer_payment_history_view"},
        ],
    })

    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=None,
        top_k=10,
        settings=phase_d_settings,
    )
    d = await comp.discover(
        "daily revenue",
        _identity_phase_d(["eunomia-finance-user", "eunomia-pii-unmask"], "finance.alice"),
    )

    names = sorted(v.name for v in d.allowed_views)
    assert names == sorted([
        "finance_daily_revenue_view", "finance_customer_payment_history_view",
    ])
    assert d.relevant_views == d.allowed_views   # no RAG, no narrowing
    # PII comes from the Mock fixture for the names returned by OM search.
    assert "finance_customer_payment_history_view" in d.pii_tags
    assert {"first_name", "last_name", "email", "card_last_four"}.issubset(
        d.pii_tags["finance_customer_payment_history_view"]
    )


async def test_phase_d_admin_bypass_query_is_wildcard(phase_d_settings, monkeypatch):
    """Admin role → q=*, fetches every view in the fixture."""
    _mock_om_search(monkeypatch, {
        # q=* is URL-encoded as q=%2A
        "q=%2A": [
            {"name": "finance_daily_revenue_view"},
            {"name": "finance_customer_payment_history_view"},
            {"name": "marketing_customer_ltv_view"},
            {"name": "marketing_regional_performance_view"},
        ],
    })
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=None,
        top_k=10,
        settings=phase_d_settings,
    )
    d = await comp.discover("everything", _identity_phase_d(["eunomia-om-admin"], "om.admin"))
    assert len(d.allowed_views) == 4


async def test_phase_d_user_without_view_roles_gets_empty_discovery(phase_d_settings, monkeypatch):
    _mock_om_search(monkeypatch, {})  # no responses configured
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=None,
        top_k=10,
        settings=phase_d_settings,
    )
    d = await comp.discover("foo", _identity_phase_d(["some-other-role"], "stranger"))
    assert d.allowed_views == []
    assert d.relevant_views == []
    assert d.pii_tags == {}


async def test_phase_d_pii_only_emitted_for_views_om_returned(phase_d_settings, monkeypatch):
    """Even though MockOpenMetadataClient knows PII for marketing views,
    discovery for an auditor (finance-only) MUST NOT leak marketing PII."""
    _mock_om_search(monkeypatch, {
        "eunomia-access.external-auditor": [
            {"name": "finance_customer_payment_history_view"},
        ],
    })
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=None,
        top_k=10,
        settings=phase_d_settings,
    )
    d = await comp.discover("payments", _identity_phase_d(["eunomia-external-auditor"], "auditor.bob"))
    assert [v.name for v in d.allowed_views] == ["finance_customer_payment_history_view"]
    # PII map keys must be a subset of the allowed view names.
    assert set(d.pii_tags.keys()).issubset({"finance_customer_payment_history_view"})


async def test_phase_d_rag_narrowing_still_works(phase_d_settings, monkeypatch):
    """If top_k < |allowed|, RAG narrowing applies on the Phase D path."""
    _mock_om_search(monkeypatch, {
        # admin sees all → 4 views, top_k=1 → RAG narrows
        "q=%2A": [
            {"name": "finance_daily_revenue_view"},
            {"name": "finance_customer_payment_history_view"},
            {"name": "marketing_customer_ltv_view"},
            {"name": "marketing_regional_performance_view"},
        ],
    })
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=MockRagClient(),
        top_k=1,
        settings=phase_d_settings,
    )
    d = await comp.discover(
        "customer lifetime value",
        _identity_phase_d(["eunomia-om-admin"], "om.admin"),
    )
    assert len(d.allowed_views) == 4
    assert len(d.relevant_views) == 1
    # MockRagClient ranks by keyword overlap; "customer lifetime value" should
    # match marketing_customer_ltv_view best.
    assert d.relevant_views[0].name == "marketing_customer_ltv_view"


# --------------------------------------------------------------------------- #
# Dispatch                                                                    #
# --------------------------------------------------------------------------- #


async def test_legacy_dispatch_when_settings_say_mock(monkeypatch):
    """When openmetadata.mock=true (default), the legacy path is used —
    no OM /search/query is attempted."""
    reset_settings_cache()
    s = load_settings()
    assert s.openmetadata.mock is True

    called = []
    def boom(*a, **kw):
        called.append(True)
        raise AssertionError("om_access.httpx.get must not be called on legacy path")
    monkeypatch.setattr(om_access.httpx, "get", boom)

    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=None,
        top_k=10,
        settings=s,
    )
    # Legacy identity shape — has 'role' (singular).
    d = await comp.discover("daily revenue", {"role": "Finance User"})
    assert called == []   # the legacy path never hit httpx
    assert len(d.allowed_views) == 2


async def test_no_settings_falls_back_to_legacy_path(monkeypatch):
    """Constructing ComposedCatalog without settings (back-compat for tests
    in test_composed_catalog.py) defaults to legacy path."""
    called = []
    monkeypatch.setattr(om_access.httpx, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call")))
    comp = ComposedCatalog(catalog=MockOpenMetadataClient(), rag=None, top_k=10)
    d = await comp.discover("foo", {"role": "Finance User"})
    assert len(d.allowed_views) == 2
