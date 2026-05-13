"""PII masking — redacts PII columns from result rows.

Unmask decision (Phase D):
    A user gets PII unmasked iff EITHER:
      • their JWT carries the compositional role configured at
        ``settings.auth.unmask_pii_role`` (default ``eunomia-pii-unmask``), OR
      • the legacy identity field ``role`` equals ``"Finance User"``
        (preserves Phase A-C behavior for the mock auth provider's
        old token strings — kept on purpose so existing tests stay
        meaningful).

Columns to mask come from ``pii_tags``: ``{view_name: [col_name, ...]}``.
We union the PII columns across views because the result columns aren't
labeled by which view they came from at the row level — when the LLM picks
ONE view per query, this is precise; for hypothetical multi-view joins, it
remains conservative (i.e., mask if ANY contributing view considers the
column PII).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


def _has_unmask(user_identity: Dict[str, Any], settings: Settings) -> bool:
    # Phase D: JWT carries the configured unmask role.
    roles = user_identity.get("roles") or []
    if settings.auth.unmask_pii_role in roles:
        return True
    # Legacy: pre-Phase-D mock tokens had role="Finance User" with implicit unmask.
    # Kept so the Phase A-C tests + offline dev flow continue to behave the same.
    if user_identity.get("role") == "Finance User":
        return True
    return False


def mask_pii(
    results: List[Dict[str, Any]],
    pii_tags: Dict[str, List[str]],
    user_identity: Dict[str, Any],
    settings: Optional[Settings] = None,
) -> List[Dict[str, Any]]:
    """Return ``results`` with PII columns redacted to ``"***"`` unless the
    user is authorized to see them unmasked.

    Args:
        results:        the rows returned from MySQL.
        pii_tags:       OM's PII map. Keys are view names; values are lists
                        of column names that should be masked.
        user_identity:  the dict produced by ``src.api.auth.verify_token``.
        settings:       optional override; defaults to the global settings.
    """
    s = settings or get_settings()

    if _has_unmask(user_identity, s):
        logger.debug(
            "mask_pii: unmask granted for user=%s",
            user_identity.get("preferred_username")
            or user_identity.get("sub")
            or user_identity.get("role"),
        )
        return results

    # Union PII columns across all views. Safer over-mask than under-mask:
    # if the same column name appears as PII in any contributing view, we
    # treat it as PII in the result.
    all_pii_cols = {c for cols in (pii_tags or {}).values() for c in cols}
    if not all_pii_cols:
        return results

    logger.debug(
        "mask_pii: masking %d PII column(s) for user=%s — %s",
        len(all_pii_cols),
        user_identity.get("preferred_username")
        or user_identity.get("sub")
        or user_identity.get("role"),
        sorted(all_pii_cols),
    )

    masked: List[Dict[str, Any]] = []
    for row in results or []:
        masked.append({
            k: ("***" if k in all_pii_cols else v)
            for k, v in row.items()
        })
    return masked
