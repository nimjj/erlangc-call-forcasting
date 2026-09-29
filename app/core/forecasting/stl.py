from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

from app.core.constants import FORECAST_COLUMNS
from app.core.ingestion.reader import read_cdr_csv
from app.core.ingestion.cleaner import preprocess_cdr, clean_percent
from app.core.ingestion.interval import build_cdr_intervals
from app.core.queuing.staffing import compute_interval_staffing


def _profile_for_day(frame: pd.DataFrame, day: pd.Timestamp, intervals_per_day: int) -> np.ndarray:
    day_start = pd.Timestamp(day).normalize()
    values = frame.loc[
        frame["interval_start"].dt.normalize() == day_start,
        ["interval_start", "call_volume"],
    ].copy()
    profile = np.zeros(intervals_per_day, dtype=float)
    if values.empty:
        return profile
    slots = (
        values["interval_start"].dt.hour * 60 + values["interval_start"].dt.minute
    ) // (1440 // intervals_per_day)
    for slot, volume in zip(slots.astype(int), values["call_volume"].astype(float)):
        profile[slot] += volume
    return profile


def _predict_day_profile(
    profiles: dict[pd.Timestamp, np.ndarray],
    day: pd.Timestamp,
) -> np.ndarray:
    prior_days = sorted(profile_day for profile_day in profiles if profile_day < day)
    if not prior_days:
        raise ValueError("At least one day of valid records is required for prediction.")

    day_number = len(prior_days)
    if day_number < 7:
        source_days = prior_days
    elif day_number < 31:
        same_weekday = [
            profile_day for profile_day in prior_days
            if profile_day.dayofweek == day.dayofweek
        ]
        source_days = same_weekday or prior_days[-7:]
    elif day_number < 366:
        same_month_position = [
            profile_day for profile_day in prior_days
            if profile_day.day == day.day and profile_day.dayofweek == day.dayofweek
        ]
        same_weekday = [
            profile_day for profile_day in prior_days
            if profile_day.dayofweek == day.dayofweek
        ]
        source_days = same_month_position or same_weekday or prior_days[-31:]
    else:
        same_year_period = [
            profile_day for profile_day in prior_days
            if profile_day.month == day.month
            and profile_day.day == day.day
            and profile_day.dayofweek == day.dayofweek
        ]
        same_month = [
            profile_day for profile_day in prior_days
            if profile_day.month == day.month
            and profile_day.dayofweek == day.dayofweek
        ]
        source_days = same_year_period or same_month or prior_days[-366:]

    return np.rint(np.mean([profiles[profile_day] for profile_day in source_days], axis=0)).astype(int)


def _forecast_window(actual_days: Sequence, requested_days: int) -> tuple[pd.Timestamp, int]:
    first = pd.Timestamp(min(actual_days)).normalize()
    last = pd.Timestamp(max(actual_days)).normalize()
    next_day = last + pd.Timedelta(days=1)
    history_days = (next_day - first).days
    if history_days < 7:
        return next_day, 1
    # Non-default API horizons remain explicit overrides.
    if requested_days != 365:
        return next_day, requested_days
    if next_day >= first + pd.DateOffset(years=1):
        start = pd.Timestamp(year=last.year + 1, month=1, day=1)
        end = start + pd.DateOffset(years=1)
        return start, (end - start).days
    if history_days >= 28:
        start = (last.to_period("M") + 1).start_time
        return start, start.days_in_month
    return next_day, 7


def build_stl_forecast(
    file_paths: Sequence[str | Path],
    filenames: Sequence[str] | None = None,
    interval_minutes: int = 30,
    forecast_days: int = 365,
    seasonal_period: int | None = None,
    trend_lookback_days: int = 90,
    target_seconds: float = 20,
    target_service_level: float = 80,
    shrinkage: float = 30,
    max_agents: int = 1000,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> tuple[pd.DataFrame, dict]:
    if not file_paths:
        raise ValueError("Upload at least one CDR dataset with one or more days of valid records.")
    if filenames is not None and len(filenames) != len(file_paths):
        raise ValueError("filenames and file_paths must have the same length.")

    interval_minutes = int(interval_minutes)
    forecast_days = int(forecast_days)
    trend_lookback_days = int(trend_lookback_days)
    if interval_minutes <= 0 or 1440 % interval_minutes != 0:
        raise ValueError("interval_minutes must be a positive divisor of 1440.")
    if forecast_days <= 0:
        raise ValueError("forecast_days must be greater than 0.")
    if trend_lookback_days < 7:
        raise ValueError("trend_lookback_days must be at least 7.")

    intervals_per_day = 1440 // interval_minutes
    resolved_period = int(seasonal_period or intervals_per_day * 7)
    if resolved_period < 2:
        raise ValueError("seasonal_period must be at least 2 intervals.")

    interval_frames: list[pd.DataFrame] = []
    dataset_summary: list[dict] = []
    historical_years: set[int] = set()
    total_files = len(file_paths)

    for index, path in enumerate(file_paths):
        filename = filenames[index] if filenames else Path(path).name
        if progress_callback:
            percent = 10 + int(30 * (index / total_files))
            progress_callback(percent, f"Reading and cleaning {filename} ({index + 1}/{total_files})...")

        raw_frame = read_cdr_csv(path)
        raw_count = len(raw_frame)
        clean = preprocess_cdr(path, raw_frame=raw_frame)
        if clean.empty:
            raise ValueError(f"No valid Answered records found in {filename}.")

        historical_years.update(clean["call_datetime"].dt.year.astype(int).unique().tolist())
        intervals = build_cdr_intervals(clean, interval_minutes=interval_minutes)
        intervals = intervals.rename(columns={"call_datetime": "interval_start"})
        interval_frames.append(intervals)
        answered_calls = int(len(clean))
        total_handle = float(clean["duration_seconds"].sum())
        dataset_summary.append({
            "filename": filename,
            "years": sorted(clean["call_datetime"].dt.year.astype(int).unique().tolist()),
            "raw_rows": int(raw_count),
            "valid_answered_calls": answered_calls,
            "removed_rows": int(raw_count - answered_calls),
            "average_aht_seconds": round(total_handle / answered_calls, 2),
            "first_call": clean["call_datetime"].min().isoformat(),
            "last_call": clean["call_datetime"].max().isoformat(),
        })

    if progress_callback:
        progress_callback(45, "Combining available records and preparing rolling prediction...")

    historical = (
        pd.concat(interval_frames, ignore_index=True)
        .groupby("interval_start", as_index=False)
        .agg(
            call_volume=("call_volume", "sum"),
            total_handle_time_seconds=("total_handle_time_seconds", "sum"),
        )
        .sort_values("interval_start")
        .reset_index(drop=True)
    )
    if historical["interval_start"].dt.normalize().nunique() < 1:
        raise ValueError("At least one day of valid records is required for prediction.")

    if progress_callback:
        progress_callback(55, "Building recursive day, week, month, and year predictions...")

    actual_days = sorted(historical["interval_start"].dt.normalize().unique())
    start, forecast_days = _forecast_window(actual_days, forecast_days)
    profiles = {
        pd.Timestamp(day): _profile_for_day(historical, pd.Timestamp(day), intervals_per_day)
        for day in actual_days
    }
    forecast_dates = [start + pd.Timedelta(days=offset) for offset in range(forecast_days)]
    future_values: list[int] = []
    for day in forecast_dates:
        profile = _predict_day_profile(profiles, day)
        profiles[day.normalize()] = profile
        future_values.extend(profile.tolist())

    future = pd.DataFrame({
        "interval_start": pd.date_range(
            start=start,
            periods=len(future_values),
            freq=f"{interval_minutes}min",
        ),
        "call_volume": np.asarray(future_values, dtype=int),
    })

    if progress_callback:
        progress_callback(65, "Estimating slot-level Average Handling Times (AHT)...")

    historical["slot"] = (
        historical["interval_start"].dt.hour * 60 + historical["interval_start"].dt.minute
    ) // interval_minutes
    total_calls = historical["call_volume"].sum()
    global_aht = (
        historical["total_handle_time_seconds"].sum() / total_calls
        if total_calls > 0 else 1.0
    )
    slot_aht = historical.groupby("slot").agg(
        total_handle=("total_handle_time_seconds", "sum"),
        calls=("call_volume", "sum"),
    )
    slot_aht["aht_seconds"] = (
        slot_aht["total_handle"] / slot_aht["calls"].replace(0, np.nan)
    ).fillna(global_aht)
    future["slot"] = (
        future["interval_start"].dt.hour * 60 + future["interval_start"].dt.minute
    ) // interval_minutes
    future["aht_seconds"] = future["slot"].map(slot_aht["aht_seconds"]).fillna(global_aht).astype(float)

    if progress_callback:
        progress_callback(75, f"Calculating Erlang C staffing across {len(future):,} intervals...")

    metrics = compute_interval_staffing(
        future_df=future,
        interval_seconds=interval_minutes * 60,
        target_seconds=float(target_seconds),
        target_service_level=float(target_service_level),
        shrinkage=float(shrinkage),
        max_agents=int(max_agents),
    )
    forecast = pd.concat([future.drop(columns=["slot"]).reset_index(drop=True), metrics], axis=1)
    forecast["date"] = forecast["interval_start"].dt.strftime("%Y-%m-%d")
    forecast["day_of_year"] = forecast["interval_start"].dt.dayofyear
    forecast["month"] = forecast["interval_start"].dt.month
    forecast["day"] = forecast["interval_start"].dt.day
    forecast["weekday"] = forecast["interval_start"].dt.dayofweek
    forecast["weekday_name"] = forecast["interval_start"].dt.day_name()
    forecast["hour"] = forecast["interval_start"].dt.hour
    forecast["minute"] = forecast["interval_start"].dt.minute
    forecast["service_level_percent"] = forecast.pop("service_level") * 100
    forecast["probability_waiting_percent"] = forecast.pop("probability_waiting") * 100
    forecast["occupancy_percent"] = forecast.pop("occupancy") * 100
    forecast = forecast.round({
        "aht_seconds": 2, "traffic_erlangs": 4, "service_level_percent": 2,
        "probability_waiting_percent": 2, "occupancy_percent": 2, "asa_seconds": 2,
    })[FORECAST_COLUMNS]

    peak_row = forecast.loc[forecast["call_volume"].idxmax()]
    target_percent = clean_percent(target_service_level) * 100
    output_year = int(forecast["interval_start"].dt.year.iloc[0])
    summary = {
        "method": "ROLLING_PROFILE",
        "logic": "Expanding day profiles, then weekly, monthly, and prior-year same-period profiles with new records included.",
        "dataset_count": len(interval_frames),
        "historical_years": sorted(historical_years),
        "historical_days": int(len(actual_days)),
        "output_year": output_year,
        "days": forecast_days,
        "interval_minutes": interval_minutes,
        "seasonal_period": resolved_period,
        "trend_lookback_days": trend_lookback_days,
        "forecast_interval_count": int(len(forecast)),
        "total_predicted_calls": int(forecast["call_volume"].sum()),
        "average_aht_seconds": round(float(np.average(
            forecast["aht_seconds"], weights=forecast["call_volume"].clip(lower=1)
        )), 2),
        "maximum_raw_agents": int(forecast["raw_agents"].max()),
        "maximum_scheduled_agents": int(forecast["scheduled_agents"].max()),
        "average_service_level_percent": round(float(forecast["service_level_percent"].mean()), 2),
        "average_occupancy_percent": round(float(forecast["occupancy_percent"].mean()), 2),
        "average_asa_seconds": round(float(forecast["asa_seconds"].mean()), 2),
        "intervals_below_service_target": int((forecast["service_level_percent"] < target_percent).sum()),
        "peak_interval": {
            "interval_start": peak_row["interval_start"].isoformat(),
            "call_volume": int(peak_row["call_volume"]),
            "scheduled_agents": int(peak_row["scheduled_agents"]),
        },
        "source_data": {
            "dataset_count": len(interval_frames),
            "historical_years": sorted(historical_years),
            "datasets": sorted(dataset_summary, key=lambda item: item["first_call"]),
        },
        "decomposition_summary": {
            "historical_intervals": int(len(historical)),
            "prediction_start": start.isoformat(),
            "prediction_strategy": "day-to-day, week-to-week, month-to-month, then year-to-year rolling profiles",
        },
    }
    return forecast, summary
