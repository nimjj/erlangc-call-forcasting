from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.schemas.schedule import MonthlyScheduleRequest
from app.core.scheduling import build_monthly_agent_schedule

router = APIRouter(prefix="/schedule", tags=["Agent Scheduling"])

@router.post(
    "/monthly",
    summary="Generate a monthly agent schedule",
    description="Create three 8-hour shifts using optional shift_start_times. Returns schedule rows and summary.shifts plus coverage counts. Assignments use start dates for overnight shifts; inspect coverage_ok for shortages.",
    responses={400: {"description": "Invalid shift times, forecast data, or insufficient agents."}},
)
def monthly_schedule(request: MonthlyScheduleRequest) -> dict:
    try:
        forecast = pd.DataFrame(request.forecast)
        if forecast.empty:
            raise ValueError("Forecast data is empty.")
        missing_columns = {"interval_start", "scheduled_agents"} - set(forecast.columns)
        if missing_columns:
            raise ValueError(f"Forecast is missing required columns: {sorted(missing_columns)}")

        schedule, summary = build_monthly_agent_schedule(
            forecast=forecast,
            year=request.year,
            month=request.month,
            agent_count=request.agent_count,
            shift_start_times=request.shift_start_times,
        )

        return {
            "summary": summary,
            "schedule": schedule.to_dict(orient="records"),
        }
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Monthly schedule generation failed: {exc}",
        ) from exc
