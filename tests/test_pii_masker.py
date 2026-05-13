"""PII masker — unmask decision via eunomia-pii-unmask role + legacy compat."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.config import load_settings, reset_settings_cache
from src.validation.pii_masker import _has_unmask, mask_pii


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings():
    reset_settings_cache()
    return load_settings()


PII_TAGS: Dict[str, List[str]] = {
    "finance_customer_payment_history_view": ["first_name", "last_name", "email", "card_last_four"],
    "marketing_customer_ltv_view":           ["email"],
}

SAMPLE_ROWS = [
    {"customer_id": "1", "first_name": "Tanya", "last_name": "Vazquez",
     "email": "alice@example.org", "amount": "236.47",
     "payment_method": "PayPal", "card_last_four": "4054"},
    {"customer_id": "2", "first_name": "Shawn", "last_name": "White",
     "email": "bob@example.com",   "amount": "112.42",
     "payment_method": "Bank Transfer", "card_last_four": None},
]


# --------------------------------------------------------------------------- #
# _has_unmask                                                                 #
# --------------------------------------------------------------------------- #


def test_unmask_phase_d_role_grants_unmask(settings):
    identity = {"roles": ["eunomia-finance-user", "eunomia-pii-unmask"]}
    assert _has_unmask(identity, settings) is True


def test_unmask_absent_in_phase_d_means_masked(settings):
    identity = {"roles": ["eunomia-external-auditor"]}
    assert _has_unmask(identity, settings) is False


def test_unmask_legacy_finance_user_role_grants_unmask(settings):
    identity = {"role": "Finance User"}
    assert _has_unmask(identity, settings) is True


def test_unmask_legacy_non_finance_role_does_not(settings):
    identity = {"role": "External Auditor"}
    assert _has_unmask(identity, settings) is False


def test_unmask_empty_identity_does_not(settings):
    assert _has_unmask({}, settings) is False


def test_unmask_either_shape_unmask_role_wins(settings):
    """Both fields present — Phase D role match alone is enough."""
    identity = {"role": "External Auditor", "roles": ["eunomia-pii-unmask"]}
    assert _has_unmask(identity, settings) is True


# --------------------------------------------------------------------------- #
# mask_pii                                                                    #
# --------------------------------------------------------------------------- #


def test_mask_pii_unmasked_user_gets_rows_unchanged(settings):
    identity = {"roles": ["eunomia-finance-user", "eunomia-pii-unmask"]}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, settings)
    assert out == SAMPLE_ROWS


def test_mask_pii_masked_user_gets_pii_redacted(settings):
    identity = {"roles": ["eunomia-external-auditor"]}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, settings)
    assert len(out) == len(SAMPLE_ROWS)
    for row in out:
        assert row["first_name"] == "***"
        assert row["last_name"] == "***"
        assert row["email"] == "***"
        assert row["card_last_four"] == "***"
        # Non-PII columns survive
        assert row["customer_id"] in {"1", "2"}
        assert row["amount"] in {"236.47", "112.42"}
        assert row["payment_method"] in {"PayPal", "Bank Transfer"}


def test_mask_pii_empty_tags_passes_through(settings):
    identity = {"roles": ["eunomia-external-auditor"]}
    out = mask_pii(SAMPLE_ROWS, {}, identity, settings)
    assert out == SAMPLE_ROWS


def test_mask_pii_empty_rows_returns_empty(settings):
    identity = {"roles": ["eunomia-external-auditor"]}
    assert mask_pii([], PII_TAGS, identity, settings) == []


def test_mask_pii_pii_set_is_union_across_views(settings):
    """email appears in BOTH PII maps. Marketing rows shouldn't accidentally
    keep email unmasked even if a non-finance user accesses them."""
    identity = {"roles": ["eunomia-marketing-lead"]}   # no pii-unmask role
    marketing_rows = [{"customer_id": "9", "email": "x@y.com", "region": "EU", "lifetime_value": "500"}]
    out = mask_pii(marketing_rows, PII_TAGS, identity, settings)
    assert out[0]["email"] == "***"
    assert out[0]["region"] == "EU"
    assert out[0]["lifetime_value"] == "500"


def test_mask_pii_legacy_finance_user_still_unmasked(settings):
    """Phase A-C backward compat: an old test using {"role": "Finance User"}
    must continue to see unmasked rows."""
    identity = {"role": "Finance User", "domain": "Finance"}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, settings)
    assert out == SAMPLE_ROWS


def test_mask_pii_admin_user_unmasked(settings):
    """om.admin has eunomia-pii-unmask in the seed; should see PII."""
    identity = {"roles": ["eunomia-om-admin", "eunomia-pii-unmask"]}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, settings)
    assert out == SAMPLE_ROWS


def test_mask_pii_agency_partner_no_pii_view_returns_unmodified_rows(settings):
    """Agency partner accesses regional_performance view (no PII columns).
    Even though they don't have the unmask role, masking is a no-op."""
    identity = {"roles": ["eunomia-agency-partner"]}
    regional_rows = [{"region": "Asia", "regional_revenue": "8399.37", "total_customers": "25"}]
    no_pii_tags: Dict[str, List[str]] = {}
    out = mask_pii(regional_rows, no_pii_tags, identity, settings)
    assert out == regional_rows


# --------------------------------------------------------------------------- #
# Settings override behavior                                                  #
# --------------------------------------------------------------------------- #


def test_mask_pii_honors_settings_override(monkeypatch):
    """If an op renames the unmask role in settings, masker tracks it."""
    monkeypatch.setenv("EUNOMIA_AUTH__UNMASK_PII_ROLE", "custom-pii-bypass")
    reset_settings_cache()
    s = load_settings()
    assert s.auth.unmask_pii_role == "custom-pii-bypass"

    # User with the renamed role
    identity = {"roles": ["custom-pii-bypass"]}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, s)
    assert out == SAMPLE_ROWS

    # User with the old role no longer gets unmasked
    identity = {"roles": ["eunomia-pii-unmask"]}
    out = mask_pii(SAMPLE_ROWS, PII_TAGS, identity, s)
    assert out[0]["email"] == "***"
