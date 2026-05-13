"""RAG client abstractions.

A RagClient takes the OM-authorized set of views and ranks them by relevance
to the user's natural-language query. The returned list is a SUBSET of the
input — RagClient implementations must never introduce a view that isn't
already in the allowed list. OpenMetadata is the source of truth for both
authorization and view metadata; RAG is purely a ranking signal.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from .base import ViewInfo


class RagUnavailable(Exception):
    """Raised when the RAG service is unreachable or returns a non-2xx.

    Callers should treat this as a soft failure: fall back to the full
    allowed-view list, log a warning, and continue.
    """


class RagClient(ABC):
    """Ranks views by relevance. Never alters authorization."""

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        allowed_views: List[ViewInfo],
        k: int,
    ) -> List[ViewInfo]:
        """Return up to ``k`` views from ``allowed_views``, sorted most-relevant first.

        Contract:
            - Result must be a subset of ``allowed_views`` (compared by ``name``).
            - Each item must be the SAME ``ViewInfo`` instance from
              ``allowed_views`` — never substitute or mutate (the indexer's
              metadata may be stale; OM is canonical).
            - Empty ``allowed_views`` → empty result.
            - On unreachable backend, raise ``RagUnavailable``.
        """
        ...
