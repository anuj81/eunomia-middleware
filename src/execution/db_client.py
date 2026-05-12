import asyncio

async def execute_query(sql: str) -> list[dict]:
    """
    Mock DB execution. In reality, use asyncpg/psycopg to execute against Postgres.
    """
    await asyncio.sleep(0.5)
    return [{"first_name": "Alice", "email": "alice@example.com", "amount": 150.0}]
