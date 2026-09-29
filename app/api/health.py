from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse
from app.config import STATIC_DIR

router = APIRouter(tags=["Health & Dashboard"])

@router.get("/")
def dashboard():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {
        "message": "CDR STL Forecast & Agent Scheduling API",
        "documentation": "/docs",
    }

@router.get("/health")
def health() -> dict:
    return {"status": "healthy", "version": "4.1.0"}
