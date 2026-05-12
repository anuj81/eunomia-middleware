import asyncio

async def generate_sql(nlq_query: str, allowed_views: list[dict], error_context: str = "") -> str:
    """
    Mock LLM integration. In reality, call OpenAI/Anthropic.
    """
    await asyncio.sleep(1.0)
    
    if error_context:
        print(f"Retrying after error: {error_context}")
        
    # Dummy mock response
    return f"SELECT * FROM {allowed_views[0]['name']} LIMIT 10;" if allowed_views else "SELECT 1"
