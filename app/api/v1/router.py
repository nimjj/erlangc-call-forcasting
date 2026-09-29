from __future__ import annotations

from fastapi import APIRouter
from app.api.v1.forecast import router as forecast_router
from app.api.v1.schedule import router as schedule_router
from app.api.v1.jobs import router as jobs_router

router = APIRouter(prefix="/api/v1")
router.include_router(forecast_router)
router.include_router(schedule_router)
router.include_router(jobs_router)
