import asyncio
from src.catalog.openmetadata import get_allowed_views_and_pii

async def main():
    print("Testing Finance User:")
    views, pii = await get_allowed_views_and_pii({"role": "Finance User"})
    for v in views:
        print(f" - View: {v['name']}, Columns: {v['columns']}")
    print(f" - PII: {pii}")
    
    print("\nTesting Marketing Lead:")
    views, pii = await get_allowed_views_and_pii({"role": "Marketing Lead"})
    for v in views:
        print(f" - View: {v['name']}, Columns: {v['columns']}")
    print(f" - PII: {pii}")

asyncio.run(main())
