from fastapi import FastAPI
from .api.routes import router

app = FastAPI(title="Eunomia Middleware")

app.include_router(router, prefix="/v1")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
