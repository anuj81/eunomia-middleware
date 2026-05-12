from fastapi import Request, HTTPException

def verify_token(request: Request) -> dict:
    # MVP: Mock Auth. In reality, decode JWT here.
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = auth_header.split(" ")[1]
    
    # Mock identities
    if token == "finance-token":
        return {"role": "Finance User", "domain": "Finance"}
    elif token == "marketing-token":
        return {"role": "Marketing Lead", "domain": "Marketing"}
    elif token == "agency-token":
        return {"role": "Agency Partner", "domain": "Marketing"}
    
    return {"role": "Unknown"}
