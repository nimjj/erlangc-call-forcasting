from __future__ import annotations

import numpy as np
import pandas as pd


def weighted_kpis(frame: pd.DataFrame) -> dict:
    """Service level and ASA weighted by calls, occupancy as total traffic over total agents.

    A plain mean counts an empty interval (100% service level, 0s ASA) the same as the busiest one.
    """
    calls = frame["call_volume"].to_numpy(dtype=float)
    agents = frame["raw_agents"].to_numpy(dtype=float).sum()
    if calls.sum() > 0:
        service_level = float(np.average(frame["service_level_percent"], weights=calls))
        asa = float(np.average(frame["asa_seconds"], weights=calls))
    else:
        service_level, asa = 100.0, 0.0
    occupancy = float(frame["traffic_erlangs"].sum() / agents * 100) if agents > 0 else 0.0
    return {
        "average_service_level_percent": service_level,
        "average_occupancy_percent": occupancy,
        "average_asa_seconds": asa,
    }


def _kpi_row(group: pd.DataFrame) -> pd.Series:
    return pd.Series(weighted_kpis(group))


def build_dashboard_aggregates(forecast: pd.DataFrame) -> dict:
    frame = forecast.copy()
    frame["interval_start"] = pd.to_datetime(frame["interval_start"])

    monthly = (
        frame.assign(
            month_number=frame["interval_start"].dt.month,
            month_label=frame["interval_start"].dt.strftime("%b"),
        )
        .groupby(["month_number", "month_label"], as_index=False)
        .agg(
            call_volume=("call_volume", "sum"),
            max_scheduled_agents=("scheduled_agents", "max"),
        )
        .sort_values("month_number")
    )
    monthly_kpis = frame.groupby(frame["interval_start"].dt.month).apply(_kpi_row)
    monthly["average_occupancy_percent"] = monthly["month_number"].map(monthly_kpis["average_occupancy_percent"])
    monthly["average_service_level_percent"] = monthly["month_number"].map(monthly_kpis["average_service_level_percent"])

    daily = (
        frame.groupby("date", as_index=False)
        .agg(
            call_volume=("call_volume", "sum"),
            max_scheduled_agents=("scheduled_agents", "max"),
        )
    )
    daily_kpis = frame.groupby("date").apply(_kpi_row)
    for column in ("average_service_level_percent", "average_occupancy_percent", "average_asa_seconds"):
        daily[column] = daily["date"].map(daily_kpis[column])

    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday = (
        frame.groupby("weekday_name", as_index=False)
        .agg(
            average_calls=("call_volume", "mean"),
            max_scheduled_agents=("scheduled_agents", "max"),
        )
        .set_index("weekday_name")
        .reindex(weekday_order)
        .reset_index()
    )

    frame["time_label"] = frame["interval_start"].dt.strftime("%H:%M")
    time_of_day = (
        frame.groupby("time_label", as_index=False)
        .agg(
            average_calls=("call_volume", "mean"),
            average_scheduled_agents=("scheduled_agents", "mean"),
        )
        .sort_values("time_label")
    )

    def records(df: pd.DataFrame) -> list[dict]:
        return df.round(2).astype(object).where(pd.notna(df), None).to_dict(orient="records")

    return {
        "monthly": records(monthly.drop(columns=["month_number"])),
        "daily": records(daily),
        "weekday": records(weekday),
        "time_of_day": records(time_of_day),
    }
