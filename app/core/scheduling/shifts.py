from __future__ import annotations
import math
import re
import pandas as pd
from app.core.constants import SHIFT_DEFINITIONS

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


def row_shift_hours(row) -> dict:
    label = str(row.get("shift", ""))
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", label):
        start = int(label[:2]) + int(label[3:5]) / 60
        end = int(label[6:8]) + int(label[9:11]) / 60
        if end <= start:
            end += 24
        if abs(end - start - 8) > 0.000001:
            raise ValueError("Each working shift must last exactly 8 hours.")
        return {"start": start, "end": end}
    shift = next(shift for shift in SHIFT_DEFINITIONS if shift["code"] == row["shift_code"])
    return {"start": shift["start_hour"], "end": shift["end_hour"]}


def build_shift_requirements(
    forecast: pd.DataFrame,
    year: int | None = None,
    month: int | None = None,
    shift_start_times: list[str] | None = None,
) -> pd.DataFrame:

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

    rows = []

    for date_value, day_frame in frame.groupby("date"):
        for shift in definitions:
            start = pd.Timestamp(date_value) + pd.Timedelta(hours=shift["start_hour"])
            end = pd.Timestamp(date_value) + pd.Timedelta(hours=shift["end_hour"])
            shift_frame = all_intervals.loc[
                (all_intervals["interval_start"] < end)
                & (all_intervals["interval_end"] > start)
            ]

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

    return pd.DataFrame(rows)

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
