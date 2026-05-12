import json
import asyncio
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

from .auth import verify_token
from ..catalog.openmetadata import get_allowed_views_and_pii
from ..generation.llm_client import generate_sql
from ..validation.sql_parser import validate_sql, SQLValidationError
from ..execution.db_client import execute_query
from ..validation.pii_masker import mask_pii

router = APIRouter()

class QueryRequest(BaseModel):
    query: str

async def execute_nlq_stream(request: Request, nlq_query: str, user_identity: dict):
    # Yield status updates
    yield {"data": json.dumps({"status": "Authenticating & Fetching Roles..."})}
    await asyncio.sleep(0.5)

    # 1. Discovery
    views, pii_tags = await get_allowed_views_and_pii(user_identity)
    yield {"data": json.dumps({"status": f"Found {len(views)} Allowed Views..."})}

    # 2. Generation & Validation Loop
    sql = None
    max_retries = 3
    error_context = ""
    
    for attempt in range(max_retries):
        yield {"data": json.dumps({"status": f"Generating SQL (Attempt {attempt + 1})...", "error_context": error_context})}
        
        generated_sql = await generate_sql(nlq_query, views, error_context)
        
        yield {"data": json.dumps({"status": "Validating generated SQL..."})}
        try:
            validate_sql(generated_sql, [v['name'] for v in views])
            sql = generated_sql
            break # Valid SQL
        except SQLValidationError as e:
            error_context = str(e)
            if attempt == max_retries - 1:
                yield {"data": json.dumps({"status": "error", "message": f"Failed to generate valid SQL: {error_context}"})}
                return

    # 3. Execution
    yield {"data": json.dumps({"status": "Executing query on Postgres..."})}
    raw_results = await execute_query(sql)

    # 4. PII Masking
    yield {"data": json.dumps({"status": "Applying PII masking..."})}
    safe_results = mask_pii(raw_results, pii_tags, user_identity)

    # 5. Complete
    yield {"event": "complete", "data": json.dumps({"results": safe_results, "executed_sql": sql})}

@router.post("/execute_nlq")
async def execute_nlq(req: QueryRequest, request: Request, user_identity: dict = Depends(verify_token)):
    return EventSourceResponse(execute_nlq_stream(request, req.query, user_identity))
