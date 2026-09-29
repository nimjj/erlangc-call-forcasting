from __future__ import annotations
from pydantic import BaseModel, Field
from app.config import MAX_FORECAST_ROWS, MAX_AGENT_COUNT

class MonthlyScheduleRequest(BaseModel):
    forecast: list[dict] = Field(min_length=1, max_length=MAX_FORECAST_ROWS)
    year: int
    month: int = Field(ge=1, le=12)
    agent_count: int | None = Field(default=None, gt=0, le=MAX_AGENT_COUNT)
    shift_start_times: list[str] | None = Field(
        default=None, min_length=3, max_length=3,
        description="Three HH:MM start times in shift order, exactly 8 hours apart. Defaults: 00:00, 08:00, 16:00. Overnight shifts belong to their start date.",
        examples=[["06:00", "14:00", "22:00"]],
    )
