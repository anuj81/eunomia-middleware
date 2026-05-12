def mask_pii(results: list[dict], pii_tags: dict, user_identity: dict) -> list[dict]:
    """
    Masks fields marked as PII if user doesn't have unmasked access.
    """
    role = user_identity.get("role")
    
    # In MVP, assume Finance User sees all, others get masking
    if role == "Finance User":
        return results
        
    masked_results = []
    # Collect all PII columns across all views for simplicity in mock
    all_pii_cols = set()
    for cols in pii_tags.values():
        all_pii_cols.update(cols)
        
    for row in results:
        masked_row = {}
        for k, v in row.items():
            if k in all_pii_cols:
                masked_row[k] = "***"
            else:
                masked_row[k] = v
        masked_results.append(masked_row)
        
    return masked_results
