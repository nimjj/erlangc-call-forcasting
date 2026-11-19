from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["Health & Service Info"])

@router.get("/")
def root() -> dict:
    return {
        "message": "CDR STL Forecast & Agent Scheduling API",
        "documentation": "/docs",
    }

@router.get("/health")
def health() -> dict:
    return {"status": "healthy", "version": "4.4.0"}
