"""Tests for src.catalog.om_access — pure helpers + HTTP path mocked.

The pure helpers (map_roles_to_tags, build_search_query, has_admin_bypass,
has_pii_unmask) are exercised directly. The OM search call is mocked at the
httpx layer so tests don't need a live OM.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from src.catalog import om_access
from src.catalog.om_access import (
    AccessDenied,
    OMAccessError,
    build_search_query,
    has_admin_bypass,
    has_pii_unmask,
    map_roles_to_tags,
    resolve_allowed_views,
)
from src.config import load_settings, reset_settings_cache


@pytest.fixture
def settings():
    reset_settings_cache()
    return load_settings()


# --------------------------------------------------------------------------- #
# pure helpers                                                                #
# --------------------------------------------------------------------------- #


def test_map_roles_to_tags_basic(settings):
    assert map_roles_to_tags(["eunomia-finance-user"], settings) == {
        "eunomia-access.finance-user"
    }


def test_map_roles_to_tags_pii_unmask_is_not_an_access_role(settings):
    # eunomia-pii-unmask is compositional, not in role_to_access_tag
    assert map_roles_to_tags(["eunomia-pii-unmask"], settings) == set()


def test_map_roles_to_tags_unknown_role_ignored(settings):
    assert map_roles_to_tags(["totally-not-a-role"], settings) == set()


def test_map_roles_to_tags_multi(settings):
    assert map_roles_to_tags(
        ["eunomia-finance-user", "eunomia-pii-unmask", "eunomia-external-auditor"],
        settings,
    ) == {"eunomia-access.finance-user", "eunomia-access.external-auditor"}


def test_map_roles_to_tags_admin_returns_wildcard(settings):
    assert map_roles_to_tags(["eunomia-om-admin"], settings) == {"*"}


def test_has_admin_bypass(settings):
    assert has_admin_bypass(["eunomia-om-admin"], settings) is True
    assert has_admin_bypass(["eunomia-finance-user"], settings) is False
    assert has_admin_bypass([], settings) is False


def test_has_pii_unmask(settings):
    assert has_pii_unmask(["eunomia-pii-unmask"], settings) is True
    assert has_pii_unmask(["eunomia-finance-user"], settings) is False


def test_build_search_query_empty():
    assert build_search_query(set()) == "*"


def test_build_search_query_single():
    assert build_search_query({"eunomia-access.finance-user"}) == \
        'tags.tagFQN:"eunomia-access.finance-user"'


def test_build_search_query_multi_is_sorted_and_or_joined():
    q = build_search_query({
        "eunomia-access.finance-user",
        "eunomia-access.external-auditor",
    })
    # sorted alphabetically for determinism
    assert q == 'tags.tagFQN:("eunomia-access.external-auditor" OR "eunomia-access.finance-user")'


# --------------------------------------------------------------------------- #
# resolve_allowed_views with mocked httpx                                     #
# --------------------------------------------------------------------------- #


def _mock_om_response(monkeypatch, captured_calls: List[Dict[str, Any]], hits_by_query):
    """Patch httpx.get to capture the call and return predetermined hits.

    hits_by_query: dict mapping substring-of-q → list of hit dicts.
    Falls back to [] for unknown queries.
    """
    def fake_get(url, headers=None, timeout=None):
        captured_calls.append({"url": url, "headers": headers})
        hits = []
        for needle, candidate_hits in hits_by_query.items():
            if needle in url:
                hits = candidate_hits
                break

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"hits": {"hits": [{"_source": h} for h in hits]}}

        return _Resp()
    monkeypatch.setattr(om_access.httpx, "get", fake_get)


def test_resolve_admin_bypass_calls_with_wildcard_query(settings, monkeypatch):
    calls: List[Dict[str, Any]] = []
    _mock_om_response(monkeypatch, calls, {
        "q=%2A": [{"name": "view_a"}, {"name": "view_b"}, {"name": "view_c"}],
    })
    identity = {
        "sub": "om.admin", "roles": ["eunomia-om-admin"], "raw_token": "admin-jwt",
        "preferred_username": "om.admin",
    }
    views = resolve_allowed_views(identity, settings)
    assert views == ["view_a", "view_b", "view_c"]
    assert len(calls) == 1
    assert "q=%2A" in calls[0]["url"]
    assert calls[0]["headers"]["Authorization"] == "Bearer admin-jwt"


def test_resolve_finance_role_uses_correct_tag_query(settings, monkeypatch):
    calls: List[Dict[str, Any]] = []
    _mock_om_response(monkeypatch, calls, {
        "eunomia-access.finance-user": [
            {"name": "finance_daily_revenue_view"},
            {"name": "finance_customer_payment_history_view"},
        ],
    })
    identity = {
        "sub": "alice", "roles": ["eunomia-finance-user", "eunomia-pii-unmask"],
        "raw_token": "alice-jwt", "preferred_username": "alice",
    }
    views = resolve_allowed_views(identity, settings)
    assert views == sorted([
        "finance_daily_revenue_view", "finance_customer_payment_history_view",
    ])
    assert len(calls) == 1
    assert "eunomia-access.finance-user" in calls[0]["url"]


def test_resolve_user_with_no_view_roles_raises(settings, monkeypatch):
    calls: List[Dict[str, Any]] = []
    _mock_om_response(monkeypatch, calls, {})
    identity = {
        "sub": "stranger", "roles": ["some-unrelated-role"],
        "raw_token": "stranger-jwt", "preferred_username": "stranger",
    }
    with pytest.raises(AccessDenied):
        resolve_allowed_views(identity, settings)
    # We should NOT have called OM at all.
    assert calls == []


def test_resolve_unauthenticated_no_roles_raises(settings):
    with pytest.raises(AccessDenied):
        resolve_allowed_views(
            {"sub": "nobody", "roles": [], "raw_token": "tok"},
            settings,
        )


def test_resolve_om_failure_raises_om_access_error(settings, monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(om_access.httpx, "get", boom)
    identity = {
        "sub": "alice", "roles": ["eunomia-finance-user"],
        "raw_token": "alice-jwt", "preferred_username": "alice",
    }
    with pytest.raises(OMAccessError):
        resolve_allowed_views(identity, settings)


def test_resolve_forwards_user_token_as_bearer(settings, monkeypatch):
    """The audit-trail property: the user's JWT (not a service account) is
    what OM sees. Verifies we don't accidentally swap in admin creds."""
    calls: List[Dict[str, Any]] = []
    _mock_om_response(monkeypatch, calls, {
        "eunomia-access.external-auditor": [{"name": "finance_customer_payment_history_view"}],
    })
    identity = {
        "sub": "bob", "roles": ["eunomia-external-auditor"],
        "raw_token": "bobs-very-own-jwt", "preferred_username": "bob",
    }
    resolve_allowed_views(identity, settings)
    assert calls[0]["headers"]["Authorization"] == "Bearer bobs-very-own-jwt"


def test_resolve_drops_wildcard_sentinel_for_non_admin(settings, monkeypatch):
    """If a non-admin role somehow mapped to '*', we MUST drop it — never
    accidentally promote a user to admin via misconfiguration."""
    # Temporarily inject a bogus mapping
    settings.authz.role_to_access_tag["broken-role"] = "*"
    settings.authz.role_to_access_tag["eunomia-finance-user"] = "eunomia-access.finance-user"
    calls: List[Dict[str, Any]] = []
    _mock_om_response(monkeypatch, calls, {
        "eunomia-access.finance-user": [{"name": "finance_daily_revenue_view"}],
    })
    identity = {
        "sub": "alice", "roles": ["broken-role", "eunomia-finance-user"],
        "raw_token": "alice-jwt", "preferred_username": "alice",
    }
    views = resolve_allowed_views(identity, settings)
    # wildcard must have been dropped; query must NOT be q=*
    assert views == ["finance_daily_revenue_view"]
    assert "q=%2A&index" not in calls[0]["url"]
    assert "eunomia-access.finance-user" in calls[0]["url"]
