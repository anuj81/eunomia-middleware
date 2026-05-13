"""MockOpenMetadataClient: role mapping, PII tags, view shape."""

import pytest

from src.catalog import MockOpenMetadataClient, ViewInfo


@pytest.fixture
def mock_om():
    return MockOpenMetadataClient()


# Role → (expected view names, expected PII-tagged views)
_CASES = [
    (
        "Finance User",
        ["finance_daily_revenue_view", "finance_customer_payment_history_view"],
        ["finance_customer_payment_history_view"],
    ),
    (
        "External Auditor",
        ["finance_customer_payment_history_view"],
        ["finance_customer_payment_history_view"],
    ),
    (
        "Marketing Lead",
        ["marketing_customer_ltv_view", "marketing_regional_performance_view"],
        ["marketing_customer_ltv_view"],
    ),
    (
        "Agency Partner",
        ["marketing_regional_performance_view"],
        [],
    ),
    ("Unknown Role", [], []),
    (None, [], []),
]


@pytest.mark.parametrize("role,expected_views,expected_pii_views", _CASES)
async def test_role_mapping(mock_om, role, expected_views, expected_pii_views):
    views, pii = await mock_om.get_allowed_views_and_pii({"role": role})
    assert [v.name for v in views] == expected_views
    assert sorted(pii.keys()) == sorted(expected_pii_views)


async def test_pii_columns_match_planning_doc(mock_om):
    """PII columns on the payment-history view must include the four
    explicitly tagged by `planning/organization_structure_example.md`."""
    _, pii = await mock_om.get_allowed_views_and_pii({"role": "Finance User"})
    assert set(pii["finance_customer_payment_history_view"]) == {
        "first_name", "last_name", "email", "card_last_four",
    }


async def test_all_views_are_viewinfo_with_columns(mock_om):
    for role in ["Finance User", "Marketing Lead", "External Auditor", "Agency Partner"]:
        views, _ = await mock_om.get_allowed_views_and_pii({"role": role})
        for v in views:
            assert isinstance(v, ViewInfo)
            assert v.columns, f"{v.name} should have columns"
            for col in v.columns:
                assert col.name


async def test_column_descriptions_present(mock_om):
    """Most fixture columns should carry descriptions — critical for the
    LLM prompt to produce sensible SQL."""
    views, _ = await mock_om.get_allowed_views_and_pii({"role": "Finance User"})
    for v in views:
        assert any(c.description for c in v.columns), v.name
