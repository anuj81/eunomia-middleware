"""ComposedCatalog: two-stage discovery, fail-open, defense-in-depth subset."""

from typing import List

import pytest

from src.catalog import (
    ComposedCatalog,
    MockOpenMetadataClient,
    MockRagClient,
    ViewInfo,
)
from src.catalog.rag_base import RagClient, RagUnavailable


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class _NeverCallRag(RagClient):
    """Fails the test if its retrieve() is ever called."""

    def __init__(self):
        self.called = False

    async def retrieve(self, query, allowed_views, k):
        self.called = True
        return []


class _BrokenRag(RagClient):
    async def retrieve(self, query, allowed_views, k):
        raise RagUnavailable("simulated outage")


class _RogueRag(RagClient):
    """Returns a view that's NOT in allowed_views — must be filtered."""

    async def retrieve(self, query, allowed_views, k):
        bogus = ViewInfo(name="not_in_allowed_set", description="")
        return [bogus] + list(allowed_views[:k - 1])


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_no_rag_returns_relevant_equals_allowed():
    catalog = MockOpenMetadataClient()
    comp = ComposedCatalog(catalog=catalog, rag=None, top_k=5)
    assert comp.rag_enabled is False
    d = await comp.discover("anything", {"role": "Finance User"})
    assert d.relevant_views == d.allowed_views
    assert len(d.allowed_views) == 2


async def test_rag_skipped_when_allowed_fits_top_k():
    """If allowed_views already fits within top_k, RAG shouldn't be called
    (latency optimization; security model doesn't depend on it)."""
    never = _NeverCallRag()
    comp = ComposedCatalog(catalog=MockOpenMetadataClient(), rag=never, top_k=10)
    d = await comp.discover("anything", {"role": "Marketing Lead"})
    assert never.called is False
    assert d.relevant_views == d.allowed_views


async def test_rag_narrows_when_allowed_exceeds_top_k():
    """Force top_k=1 so RAG is consulted to pick the best one."""
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=MockRagClient(),
        top_k=1,
    )
    d = await comp.discover(
        "customer email lookup", {"role": "Marketing Lead"},
    )
    assert len(d.allowed_views) == 2
    assert len(d.relevant_views) == 1
    assert d.relevant_views[0].name == "marketing_customer_ltv_view"


async def test_rag_unavailable_falls_back_to_full_allowed():
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=_BrokenRag(),
        top_k=1,  # would normally narrow
    )
    d = await comp.discover("revenue", {"role": "Finance User"})
    # Fail-open: relevant == allowed even though RAG raised
    assert d.relevant_views == d.allowed_views


async def test_rogue_rag_views_filtered():
    """If a misbehaving RagClient returns a view that wasn't in the allowed
    set, ComposedCatalog must filter it out (defense in depth)."""
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=_RogueRag(),
        top_k=1,
    )
    d = await comp.discover("anything", {"role": "Finance User"})
    relevant_names = [v.name for v in d.relevant_views]
    assert "not_in_allowed_set" not in relevant_names


async def test_unknown_role_returns_empty_discovery_without_calling_rag():
    never = _NeverCallRag()
    comp = ComposedCatalog(catalog=MockOpenMetadataClient(), rag=never, top_k=1)
    d = await comp.discover("anything", {"role": "Unknown"})
    assert d.allowed_views == []
    assert d.relevant_views == []
    assert never.called is False  # short-circuited on empty allowed


async def test_pii_tags_authoritative_from_om():
    """The pii_tags returned by discover() must be the OM payload verbatim,
    regardless of whether RAG was consulted."""
    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=MockRagClient(),
        top_k=1,
    )
    d = await comp.discover("payment history", {"role": "External Auditor"})
    assert set(d.pii_tags["finance_customer_payment_history_view"]) == {
        "first_name", "last_name", "email", "card_last_four",
    }


# --------------------------------------------------------------------------- #
# Defense-in-depth: validator gets the FULL allowed list                      #
# --------------------------------------------------------------------------- #


async def test_validator_accepts_om_allowed_view_outside_rag_topk():
    """The key invariant of Phase C:

    The LLM prompt sees only the RAG-narrowed top-K, but the SQL validator
    must accept any view that OpenMetadata authorized, even if it didn't
    make the RAG top-K. Otherwise the security model would over-fit the
    RAG ranking, which is just a relevance signal."""
    from src.validation.sql_parser import validate_sql

    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=MockRagClient(),
        top_k=1,  # narrow to one
    )
    d = await comp.discover("daily revenue", {"role": "Finance User"})
    assert len(d.allowed_views) == 2
    assert len(d.relevant_views) == 1
    relevant_name = d.relevant_views[0].name

    # The OTHER allowed view didn't make top-K — find it:
    other = next(
        v.name for v in d.allowed_views if v.name != relevant_name
    )
    full_allowed_names: List[str] = [v.name for v in d.allowed_views]

    # LLM picks the non-top-K view — validator must accept.
    sql = f"SELECT * FROM {other}"
    assert validate_sql(sql, full_allowed_names) is True


async def test_validator_rejects_base_table_even_when_in_topk():
    """And it still rejects truly unauthorized views regardless of RAG output."""
    from src.validation.sql_parser import (
        SQLValidationError, validate_sql,
    )

    comp = ComposedCatalog(
        catalog=MockOpenMetadataClient(),
        rag=MockRagClient(),
        top_k=2,
    )
    d = await comp.discover("anything", {"role": "Finance User"})
    full_allowed_names = [v.name for v in d.allowed_views]
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT * FROM core_dim_customers", full_allowed_names)
