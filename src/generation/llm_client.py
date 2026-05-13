"""LLM (Gemini) client. Reads model and API key from settings.llm.

API key precedence (handled by Settings loader):
    1. CLI overrides
    2. GEMINI_API_KEY env var (well-known short form)
    3. EUNOMIA_LLM__API_KEY env var
    4. NEVER from YAML — secrets are rejected at load time
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from google import genai

from ..catalog import ViewInfo
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


def _build_schema_context(views: List[ViewInfo]) -> str:
    lines = [
        "You are Eunomia, a strict SQL generator. You must ONLY output a "
        "valid MySQL SELECT query. Do not use Markdown formatting like ```sql. "
        "Just the raw query.",
        "",
        "You have access to the following tables/views:",
    ]
    for v in views:
        desc = f" — {v.description}" if v.description else ""
        lines.append(f"- {v.name}{desc}")
        for col in v.columns:
            col_desc = f": {col.description}" if col.description else ""
            lines.append(f"    • {col.name}{col_desc}")
    return "\n".join(lines) + "\n"


async def generate_sql(
    nlq_query: str,
    allowed_views: List[ViewInfo],
    error_context: str = "",
    settings: Optional[Settings] = None,
) -> str:
    """Generate a MySQL SELECT for the given NLQ.

    If no API key is configured we emit a deterministic mock SQL targeting
    the first allowed view — useful for offline dev.
    """
    s = settings or get_settings()
    cfg = s.llm

    if not cfg.api_key:
        logger.warning(
            "LLM api_key not set — emitting mock SQL targeting the first "
            "allowed view. Set GEMINI_API_KEY in your environment."
        )
        await asyncio.sleep(1.0)
        return (
            f"SELECT * FROM {allowed_views[0].name} LIMIT 10;"
            if allowed_views else "SELECT 1"
        )

    client = genai.Client(api_key=cfg.api_key)
    schema_context = _build_schema_context(allowed_views)
    prompt = f"{schema_context}\nUser Question: {nlq_query}\n"
    if error_context:
        prompt += (
            f"\nYour previous attempt failed with error: {error_context}\n"
            "Please fix the SQL syntax or table references and try again.\n"
        )

    try:
        response = await client.aio.models.generate_content(
            model=cfg.model,
            contents=prompt,
        )
        sql = response.text.strip()
        # Strip Markdown code fences if the LLM ignores instructions.
        if sql.startswith("```sql"):
            sql = sql.strip("`").replace("sql\n", "", 1).strip()
        elif sql.startswith("```"):
            sql = sql.strip("`").strip()
        return sql
    except Exception:
        logger.exception("Gemini API call failed (model=%s)", cfg.model)
        raise
