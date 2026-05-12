import asyncio

async def get_allowed_views_and_pii(user_identity: dict) -> tuple[list[dict], dict]:
    """
    Mock integration with OpenMetadata. Returns allowed tools and PII map.
    """
    await asyncio.sleep(0.5) # Simulate network call
    
    role = user_identity.get("role")
    
    pii_tags = {
        "finance.customer_payment_history_view": ["first_name", "last_name", "email", "card_last_four"]
    }
    
    if role == "Finance User":
        views = [
            {"name": "finance.daily_revenue_view", "description": "Get daily revenue"},
            {"name": "finance.customer_payment_history_view", "description": "Get payment history"}
        ]
    elif role == "Marketing Lead":
        views = [
            {"name": "marketing.customer_ltv_view", "description": "Customer LTV"},
            {"name": "marketing.regional_performance_view", "description": "Regional performance"}
        ]
    elif role == "Agency Partner":
        views = [
            {"name": "marketing.regional_performance_view", "description": "Regional performance"}
        ]
    else:
        views = []
        
    return views, pii_tags
