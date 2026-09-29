from __future__ import annotations

import pandas as pd

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
            average_occupancy_percent=("occupancy_percent", "mean"),
            average_service_level_percent=("service_level_percent", "mean"),
        )
        .sort_values("month_number")
    )

    daily = (
        frame.groupby("date", as_index=False)
        .agg(
            call_volume=("call_volume", "sum"),
            weighted_handle_time=("aht_seconds", lambda values: 0.0),
            max_scheduled_agents=("scheduled_agents", "max"),
            average_service_level_percent=("service_level_percent", "mean"),
            average_occupancy_percent=("occupancy_percent", "mean"),
            average_asa_seconds=("asa_seconds", "mean"),
        )
    )
    daily = daily.drop(columns=["weighted_handle_time"])

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
        return df.round(2).where(pd.notna(df), None).to_dict(orient="records")

    return {
        "monthly": records(monthly.drop(columns=["month_number"])),
        "daily": records(daily),
        "weekday": records(weekday),
        "time_of_day": records(time_of_day),
    }
