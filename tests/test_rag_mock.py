"""MockRagClient: keyword ranking, tie-break, edge cases, subset invariant."""

import pytest

from src.catalog import ColumnInfo, ViewInfo
from src.catalog.rag_mock import MockRagClient, _tokenize


@pytest.fixture
def views():
    return [
        ViewInfo(
            name="finance_daily_revenue_view",
            description="Aggregated daily revenue rollup",
            columns=[
                ColumnInfo(name="day", description="UTC date"),
                ColumnInfo(name="gross_revenue", description="Sum of order totals"),
            ],
        ),
        ViewInfo(
            name="marketing_customer_ltv_view",
            description="Customer lifetime value with email PII",
            columns=[
                ColumnInfo(name="customer_id"),
                ColumnInfo(name="email"),
                ColumnInfo(name="ltv", description="Lifetime value"),
            ],
        ),
        ViewInfo(
            name="marketing_regional_performance_view",
            description="Regional sales metrics by area",
            columns=[
                ColumnInfo(name="region"),
                ColumnInfo(name="total_revenue", description="Revenue per region"),
            ],
        ),
    ]


def test_tokenizer_drops_stopwords():
    assert _tokenize("what is the daily revenue last week") == {
        "daily", "revenue", "last", "week",
    }
    # Single-char tokens dropped
    assert "a" not in _tokenize("a b c d ef")


def test_tokenizer_handles_punctuation_and_case():
    assert _tokenize("Daily-Revenue, last_week!") == {"daily", "revenue", "last", "week"}


async def test_ranks_relevant_view_first(views):
    rag = MockRagClient()
    ranked = await rag.retrieve("daily revenue per day", views, k=3)
    assert ranked[0].name == "finance_daily_revenue_view"


async def test_returns_top_k(views):
    rag = MockRagClient()
    ranked = await rag.retrieve("email customer", views, k=1)
    assert len(ranked) == 1
    assert ranked[0].name == "marketing_customer_ltv_view"


async def test_empty_views_returns_empty():
    rag = MockRagClient()
    assert await rag.retrieve("anything", [], k=5) == []


async def test_k_zero_returns_empty(views):
    rag = MockRagClient()
    assert await rag.retrieve("daily revenue", views, k=0) == []


async def test_no_signal_query_preserves_order(views):
    """If the query has no useful tokens (all stopwords), keep insertion order."""
    rag = MockRagClient()
    ranked = await rag.retrieve("the of and in", views, k=2)
    assert [v.name for v in ranked] == [views[0].name, views[1].name]


async def test_ties_break_by_insertion_order():
    """Two views with identical scores → insertion order wins."""
    a = ViewInfo(name="a_view", description="alpha", columns=[])
    b = ViewInfo(name="b_view", description="alpha", columns=[])
    rag = MockRagClient()
    # 'alpha' appears in both; ties → original order
    ranked = await rag.retrieve("alpha", [a, b], k=2)
    assert [v.name for v in ranked] == ["a_view", "b_view"]


async def test_returns_identical_instances(views):
    """Mock must return the SAME ViewInfo instances, not deep copies."""
    rag = MockRagClient()
    ranked = await rag.retrieve("daily revenue", views, k=1)
    assert ranked[0] is views[0]


async def test_subset_invariant(views):
    """Returned views are always a subset of the input (by name)."""
    rag = MockRagClient()
    allowed_names = {v.name for v in views}
    ranked = await rag.retrieve("anything", views, k=10)
    assert {v.name for v in ranked}.issubset(allowed_names)
