from __future__ import annotations
import math
import re
import numpy as np
import pandas as pd
from app.core.constants import SHIFT_DEFINITIONS
from app.core.queuing.peak_staffing import simulate_shift_agents

def shift_definitions(start_times: list[str] | None = None) -> list[dict]:
    if start_times is None:
        return [dict(shift) for shift in SHIFT_DEFINITIONS]
    if len(start_times) != 3 or any(
        not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
        for value in start_times
    ):
        raise ValueError("Enter exactly three shift start times in HH:MM format.")
    minutes = [int(value[:2]) * 60 + int(value[3:]) for value in start_times]
    if any((minutes[(index + 1) % 3] - minutes[index]) % 1440 != 480 for index in range(3)):
        raise ValueError("Shifts must start 8 hours apart, in shift order, with no gaps or overlaps.")
    result = []
    for index, minute in enumerate(minutes):
        end = (minute + 480) % 1440
        result.append({
            **SHIFT_DEFINITIONS[index],
            "start_hour": minute / 60,
            "end_hour": (minute + 480) / 60,
            "label": f"{start_times[index]}-{end // 60:02d}:{end % 60:02d}",
        })
    return result


def build_shift_requirements(
    forecast: pd.DataFrame,
    year: int | None = None,
    month: int | None = None,
    shift_start_times: list[str] | None = None,
    coverage_percent: float | None = None,
    night_minimum_agents: int = 0,
    weekend_minimum_agents: int = 0,
    target_seconds: float = 20,
    target_service_level: float = 80,
    shrinkage: float = 30,
    max_agents: int = 1000,
) -> pd.DataFrame:
    """Agents required per shift.

    Without coverage_percent a shift needs the largest scheduled_agents among its intervals.
    With it, the shift is sized so that share of simulated shifts is fully covered (see
    app.core.queuing.peak_staffing); this needs call_volume and aht_seconds in the forecast.
    The minimums then raise night shifts and weekend day shifts to at least that many agents.
    """

    if forecast.empty:
        raise ValueError("Forecast is empty.")

    if "interval_start" not in forecast.columns:
        raise ValueError("Forecast must contain interval_start.")

    if "scheduled_agents" not in forecast.columns:
        raise ValueError("Forecast must contain scheduled_agents.")

    definitions = shift_definitions(shift_start_times)
    frame = forecast.copy()
    frame["interval_start"] = pd.to_datetime(frame["interval_start"])

    all_intervals = frame.sort_values("interval_start").copy()
    spacing = all_intervals["interval_start"].drop_duplicates().diff().dropna()
    interval = spacing.min() if not spacing.empty else pd.Timedelta(minutes=30)
    all_intervals["interval_end"] = all_intervals["interval_start"] + interval

    if year is not None:
        frame = frame.loc[frame["interval_start"].dt.year == int(year)]

    if month is not None:
        if not 1 <= int(month) <= 12:
            raise ValueError("month must be between 1 and 12.")

        frame = frame.loc[frame["interval_start"].dt.month == int(month)]

    if frame.empty:
        raise ValueError("No forecast rows found for the selected month.")

    frame["date"] = frame["interval_start"].dt.date
    frame["hour"] = frame["interval_start"].dt.hour

    if coverage_percent is not None:
        missing = {"call_volume", "aht_seconds"} - set(all_intervals.columns)
        if missing:
            raise ValueError(
                f"Forecast is missing {sorted(missing)}, which coverage_percent needs; "
                "send the full forecast rows or set coverage_percent to null."
            )

    rows = []
    shift_frames = []

    for date_value, day_frame in frame.groupby("date"):
        for shift in definitions:
            start = pd.Timestamp(date_value) + pd.Timedelta(hours=shift["start_hour"])
            end = pd.Timestamp(date_value) + pd.Timedelta(hours=shift["end_hour"])
            shift_frame = all_intervals.loc[
                (all_intervals["interval_start"] < end)
                & (all_intervals["interval_end"] > start)
            ]
            shift_frames.append(shift_frame)

            required_agents = (
                int(shift_frame["scheduled_agents"].max())
                if not shift_frame.empty
                else 0
            )

            rows.append(
                {
                    "date": pd.Timestamp(date_value),
                    "weekday": pd.Timestamp(date_value).day_name(),
                    "shift_code": shift["code"],
                    "shift_name": shift["name"],
                    "shift_label": shift["label"],
                    "start_hour": shift["start_hour"],
                    "end_hour": shift["end_hour"],
                    "required_agents": required_agents,
                }
            )

    requirements = pd.DataFrame(rows)

    if coverage_percent is not None:
        has_intervals = np.array([not shift_frame.empty for shift_frame in shift_frames])
        if has_intervals.any():
            used = [shift_frame for shift_frame in shift_frames if not shift_frame.empty]
            sizes = [len(shift_frame) for shift_frame in used]
            combined = pd.concat(used)
            requirements.loc[has_intervals, "required_agents"] = simulate_shift_agents(
                volume=combined["call_volume"].to_numpy(),
                aht_seconds=combined["aht_seconds"].to_numpy(),
                group_starts=np.cumsum([0] + sizes[:-1]),
                coverage_percent=float(coverage_percent),
                interval_seconds=int(interval.total_seconds()),
                target_seconds=float(target_seconds),
                target_service_level=float(target_service_level),
                shrinkage=float(shrinkage),
                max_agents=int(max_agents),
            )

    is_night = requirements["shift_code"] == "NIGHT"
    is_weekend_day = ~is_night & (requirements["date"].dt.dayofweek >= 5)
    requirements.loc[is_night, "required_agents"] = requirements.loc[is_night, "required_agents"].clip(lower=int(night_minimum_agents))
    requirements.loc[is_weekend_day, "required_agents"] = requirements.loc[is_weekend_day, "required_agents"].clip(lower=int(weekend_minimum_agents))
    return requirements

def calculate_schedule_headcount(
    shift_requirements: pd.DataFrame,
    working_days_per_week: int = 5,
) -> int:

    if shift_requirements.empty:
        return 0

    if working_days_per_week <= 0 or working_days_per_week > 7:
        raise ValueError("working_days_per_week must be between 1 and 7.")

    frame = shift_requirements.copy()
    frame["date"] = pd.to_datetime(frame["date"])

    required_headcount = 0

    frame["week_start"] = (
        frame["date"]
        - pd.to_timedelta(frame["date"].dt.weekday, unit="D")
    )

    for _, week in frame.groupby("week_start"):
        total_weekly_shift_slots = int(week["required_agents"].sum())

        weekly_capacity_headcount = math.ceil(
            total_weekly_shift_slots / working_days_per_week
        )

        daily_requirements = (
            week.groupby("date")["required_agents"]
            .sum()
        )

        maximum_daily_agents = (
            int(daily_requirements.max())
            if not daily_requirements.empty
            else 0
        )

        required_headcount = max(
            required_headcount,
            weekly_capacity_headcount,
            maximum_daily_agents,
        )

    return required_headcount
