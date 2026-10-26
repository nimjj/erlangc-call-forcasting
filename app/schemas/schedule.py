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
    coverage_percent: float | None = Field(
        default=80, ge=50, le=99,
        description="Share of shifts that should be fully covered. Each shift is sized to the busiest interval it is likely to see (simulated from call_volume and aht_seconds). null uses the largest scheduled_agents in the shift instead.",
    )
    night_minimum_agents: int = Field(
        default=3, ge=0, le=MAX_AGENT_COUNT,
        description="Minimum agents on the night shift, even when no calls are forecast. 0 turns it off.",
    )
    weekend_minimum_agents: int = Field(
        default=3, ge=0, le=MAX_AGENT_COUNT,
        description="Minimum agents on Saturday and Sunday day shifts. 0 turns it off.",
    )
    target_seconds: float = Field(default=20, gt=0, description="Answer-time target for the simulated shifts; use the value the forecast was made with.")
    target_service_level: float = Field(default=80, gt=0, le=100, description="Service-level target (percent) for the simulated shifts.")
    shrinkage: float = Field(default=30, ge=0, lt=100, description="Shrinkage (percent) for the simulated shifts.")
