"""Deterministic mock RagClient.

Scores each ViewInfo by token overlap between the query and the view's
synthesized text (name + description + column names + column descriptions).
No HTTP, no Qdrant. Useful for:
    - dev runs without eunomia-rag up
    - CI tests
    - the failover path: when the real RAG service is down, the middleware
      could be configured to fall back to this mock for graceful degradation
      (though the default fail-open is to skip ranking entirely).
"""

from __future__ import annotations

import logging
import re
from typing import List, Set

from .base import ViewInfo
from .rag_base import RagClient

logger = logging.getLogger(__name__)


# Very small English stopword list — keeps the mock self-contained without
# pulling NLTK/spacy. Skipping these makes "what is daily revenue" tokenize
# closer to "daily revenue", which lines up better with view names.
_STOPWORDS: Set[str] = {
    "the", "a", "an", "and", "or", "but", "if", "of", "in", "on", "at",
    "to", "for", "from", "with", "by", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "should", "could", "what", "which", "who", "whom", "this",
    "that", "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "what's", "show", "me", "list", "all", "some", "any", "no",
}


def _tokenize(text: str) -> Set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords and 1-char tokens."""
    raw = re.split(r"[^a-zA-Z0-9]+", (text or "").lower())
    return {t for t in raw if len(t) > 1 and t not in _STOPWORDS}


def _doc_tokens(view: ViewInfo) -> Set[str]:
    parts = [view.name, view.description]
    parts.extend(c.name for c in view.columns)
    parts.extend(c.description for c in view.columns)
    return _tokenize(" ".join(parts))


class MockRagClient(RagClient):
    async def retrieve(
        self,
        query: str,
        allowed_views: List[ViewInfo],
        k: int,
    ) -> List[ViewInfo]:
        if not allowed_views or k <= 0:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            # No useful query signal — return the first k as-is rather than
            # arbitrary ordering.
            return list(allowed_views[:k])

        # Score = |query_tokens ∩ doc_tokens|.
        # Tie-break by original order so behavior is fully deterministic.
        scored = [
            (len(q_tokens & _doc_tokens(v)), idx, v)
            for idx, v in enumerate(allowed_views)
        ]
        scored.sort(key=lambda s: (-s[0], s[1]))
        ranked = [v for _, _, v in scored[:k]]
        logger.debug(
            "MockRagClient: q_tokens=%s → top-%d %s",
            sorted(q_tokens), k, [v.name for v in ranked],
        )
        return ranked
