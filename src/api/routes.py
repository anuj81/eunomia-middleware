"""HTTP routes for Eunomia Middleware.

POST /v1/execute_nlq — accepts a natural language question, returns an SSE
stream of {status, ...} events ending with an "complete" event carrying the
masked results plus the executed SQL.

Phase C wiring:
    discover() → CatalogDiscovery
        ├── allowed_views   (full OM list — handed to SQL validator)
        ├── relevant_views  (RAG-narrowed — handed to LLM prompt builder)
        └── pii_tags        (OM-authoritative — handed to PII masker)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..catalog import ComposedCatalog, fastapi_composed_catalog
from ..config import get_settings
from ..execution.db_client import execute_query
from ..generation.llm_client import generate_sql
from ..validation.pii_masker import mask_pii
from ..validation.sql_parser import SQLValidationError, validate_sql
from .auth import verify_token

logger = logging.getLogger(__name__)
router = APIRouter()


class QueryRequest(BaseModel):
    query: str


async def execute_nlq_stream(
    nlq_query: str,
    user_identity: dict,
    catalog: ComposedCatalog,
) -> AsyncIterator[dict]:
    logger.info(
        "NLQ received | role=%s | query=%r",
        user_identity.get("role"), nlq_query,
    )

    yield {"data": json.dumps({"status": "Authenticating & Fetching Roles..."})}
    await asyncio.sleep(0.5)

    # 1. Discovery — OM authorizes, RAG (optionally) narrows for the prompt.
    discovery = await catalog.discover(nlq_query, user_identity)
    allowed_view_names = [v.name for v in discovery.allowed_views]   # <- for VALIDATOR
    prompt_views = discovery.relevant_views                          # <- for LLM
    pii_tags = discovery.pii_tags                                    # <- for MASKER
    logger.debug(
        "Discovery | allowed=%s | relevant=%s",
        allowed_view_names, [v.name for v in prompt_views],
    )
    yield {"data": json.dumps({
        "status": (
            f"Found {len(allowed_view_names)} Allowed Views, "
            f"using top-{len(prompt_views)} for prompt..."
            if len(prompt_views) != len(allowed_view_names)
            else f"Found {len(allowed_view_names)} Allowed Views..."
        )
    })}

    # 2. Generation + Validation loop.
    #
    # IMPORTANT defense-in-depth invariant:
    #     - The prompt sees ONLY `prompt_views` (top-K).
    #     - The validator checks against ALL `allowed_view_names` (full OM list).
    # This means the LLM can legitimately pick a view that wasn't in the
    # top-K but IS in the OM-allowed list — the validator accepts. It can
    # never pick a view outside the allowed list.
    sql = None
    raw_results = None
    max_retries = get_settings().llm.max_retries
    error_context = ""

    for attempt in range(max_retries):
        yield {"data": json.dumps({
            "status": f"Generating SQL (Attempt {attempt + 1})...",
            "error_context": error_context,
        })}

        try:
            generated_sql = await generate_sql(nlq_query, prompt_views, error_context)
            logger.info("Generated SQL (attempt %d):\n%s", attempt + 1, generated_sql)
        except Exception as e:
            logger.exception("LLM generation failed")
            yield {"data": json.dumps({
                "status": "error",
                "message": f"LLM Generation failed: {e}",
            })}
            return

        yield {"data": json.dumps({"status": "Validating generated SQL..."})}
        try:
            validate_sql(generated_sql, allowed_view_names)  # FULL list
            sql = generated_sql
        except SQLValidationError as e:
            error_context = f"Validation Error: {e}"
            logger.warning("SQL validation rejected attempt %d: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                yield {"data": json.dumps({
                    "status": "error",
                    "message": f"Failed to generate valid SQL: {error_context}",
                })}
                return
            continue

        # 3. Execute
        yield {"data": json.dumps({"status": "Executing query on MySQL..."})}
        try:
            raw_results = await execute_query(sql)
            break
        except Exception as e:
            error_context = f"MySQL Database Error: {e}"
            logger.warning("MySQL execution rejected attempt %d: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                yield {"data": json.dumps({
                    "status": "error",
                    "message": f"Database Execution Failed: {error_context}",
                })}
                return
            continue

    # 4. PII masking
    yield {"data": json.dumps({"status": "Applying PII masking..."})}
    safe_results = mask_pii(raw_results, pii_tags, user_identity)

    # 5. Done
    logger.info("NLQ complete | role=%s | rows=%d",
                user_identity.get("role"), len(safe_results))
    yield {"event": "complete", "data": json.dumps({
        "results": safe_results,
        "executed_sql": sql,
    })}


@router.post("/execute_nlq")
async def execute_nlq(
    req: QueryRequest,
    user_identity: dict = Depends(verify_token),
    catalog: ComposedCatalog = Depends(fastapi_composed_catalog),
):
    return EventSourceResponse(
        execute_nlq_stream(req.query, user_identity, catalog)
    )
