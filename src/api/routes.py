"""HTTP routes for Eunomia Middleware.

POST /v1/execute_nlq — accepts a natural language question, returns an SSE
stream of {status, ...} events ending with an "complete" event carrying the
masked results plus the executed SQL.

Phase C wiring:
    discover() → CatalogDiscovery
        ├── allowed_views   (full OM list — handed to SQL validator)
        ├── relevant_views  (RAG-narrowed — handed to LLM prompt builder)
        └── pii_tags        (OM-authoritative — handed to PII masker)

Phase D wiring:
    • verify_token returns the Keycloak JWT identity with roles + raw_token.
    • Per-request AuditRecord accumulates the trust trail and is emitted once
      at the SSE terminal event (success, error, or denied).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..audit_log import AuditRecord, get_audit_logger
from ..catalog import ComposedCatalog, fastapi_composed_catalog
from ..catalog.om_access import has_admin_bypass, has_pii_unmask
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
    settings = get_settings()

    # Audit record begins now; emitted at every terminal path (return).
    audit = AuditRecord.start(identity=user_identity, query=nlq_query)
    roles_list = user_identity.get("roles") or []
    audit.unmask_pii   = has_pii_unmask(roles_list, settings)
    audit.admin_bypass = has_admin_bypass(roles_list, settings)
    audit_logger = get_audit_logger()

    who = (user_identity.get("preferred_username")
           or user_identity.get("sub")
           or user_identity.get("role")
           or "(anonymous)")
    logger.info(
        "NLQ received | id=%s user=%s roles=%s query=%r",
        audit.request_id[:8], who,
        user_identity.get("roles") or user_identity.get("role"), nlq_query,
    )

    def _emit_audit(status: str, error: str | None = None):
        audit.finish(status=status, error=error)
        audit_logger.emit(audit)

    yield {"data": json.dumps({"status": "Authenticating & Fetching Roles..."})}
    await asyncio.sleep(0.5)

    # 1. Discovery — OM authorizes, RAG (optionally) narrows for the prompt.
    discovery = await catalog.discover(nlq_query, user_identity)
    allowed_view_names = [v.name for v in discovery.allowed_views]   # <- for VALIDATOR
    prompt_views = discovery.relevant_views                          # <- for LLM
    pii_tags = discovery.pii_tags                                    # <- for MASKER

    audit.allowed_views  = list(allowed_view_names)
    audit.relevant_views = [v.name for v in prompt_views]
    audit.pii_columns    = {k: list(v) for k, v in (pii_tags or {}).items()}

    if not allowed_view_names:
        logger.warning("Discovery empty — user has no authorized views.")
        _emit_audit("denied", error="No authorized views")
        yield {"event": "complete", "data": json.dumps({
            "status": "error",
            "message": (
                "No views are authorized for your identity. Contact your "
                "OpenMetadata admin if you believe this is incorrect."
            ),
        })}
        return

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
    # Defense-in-depth invariant:
    #     - The prompt sees ONLY `prompt_views` (top-K).
    #     - The validator checks against ALL `allowed_view_names` (full OM list).
    sql = None
    raw_results = None
    max_retries = settings.llm.max_retries
    error_context = ""

    for attempt in range(max_retries):
        audit.validation_attempts = attempt + 1
        yield {"data": json.dumps({
            "status": f"Generating SQL (Attempt {attempt + 1})...",
            "error_context": error_context,
        })}

        try:
            generated_sql = await generate_sql(nlq_query, prompt_views, error_context)
            logger.info("Generated SQL (attempt %d):\n%s", attempt + 1, generated_sql)
        except Exception as e:
            logger.exception("LLM generation failed")
            _emit_audit("error", error=f"LLM Generation failed: {e}")
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
            audit.validation_errors.append(str(e))
            logger.warning("SQL validation rejected attempt %d: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                _emit_audit("error", error=f"Failed to generate valid SQL: {error_context}")
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
                _emit_audit("error", error=f"Database Execution Failed: {error_context}")
                yield {"data": json.dumps({
                    "status": "error",
                    "message": f"Database Execution Failed: {error_context}",
                })}
                return
            continue

    audit.executed_sql = sql
    audit.rows_returned = len(raw_results) if raw_results else 0

    # 4. PII masking
    yield {"data": json.dumps({"status": "Applying PII masking..."})}
    safe_results = mask_pii(raw_results, pii_tags, user_identity, settings)
    if not audit.unmask_pii:
        # The columns the masker actually redacted.
        all_pii_cols = sorted({c for cols in (pii_tags or {}).values() for c in cols})
        audit.pii_columns_masked = all_pii_cols

    # 5. Done
    logger.info("NLQ complete | id=%s user=%s rows=%d",
                audit.request_id[:8], who, len(safe_results))
    _emit_audit("ok")
    yield {"event": "complete", "data": json.dumps({
        "results": safe_results,
        "executed_sql": sql,
        "request_id": audit.request_id,
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
