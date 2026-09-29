from __future__ import annotations

import pandas as pd
from app.core.ingestion.cleaner import infer_single_year

def build_cdr_intervals(
    clean_df: pd.DataFrame,
    interval_minutes: int = 60,
    include_empty_intervals: bool = False,
    year: int | None = None,
) -> pd.DataFrame:
    if clean_df.empty:
        raise ValueError("No clean rows available for interval forecast.")
    interval_minutes = int(interval_minutes)
    if interval_minutes <= 0 or 1440 % interval_minutes != 0:
        raise ValueError("interval_minutes must be a positive divisor of 1440.")

    rule = f"{interval_minutes}min"
    interval_df = (
        clean_df.set_index("call_datetime")
        .resample(rule)
        .agg(
            call_volume=("duration_seconds", "size"),
            total_handle_time_seconds=("duration_seconds", "sum"),
        )
        .reset_index()
    )

    if include_empty_intervals:
        resolved_year = int(year or infer_single_year(clean_df))
        full_index = pd.date_range(
            start=f"{resolved_year}-01-01 00:00:00",
            end=f"{resolved_year + 1}-01-01 00:00:00",
            freq=rule,
            inclusive="left",
        )
        interval_df = (
            interval_df.set_index("call_datetime")
            .reindex(full_index, fill_value=0)
            .rename_axis("call_datetime")
            .reset_index()
        )
    else:
        interval_df = interval_df.loc[interval_df["call_volume"] > 0].copy()

    interval_df["call_volume"] = interval_df["call_volume"].astype(int)
    interval_df["total_handle_time_seconds"] = (
        interval_df["total_handle_time_seconds"].fillna(0).astype(float)
    )
    interval_df["aht_seconds"] = (
        interval_df["total_handle_time_seconds"]
        .div(interval_df["call_volume"].replace(0, pd.NA))
        .astype("Float64")
    )
    interval_df["interval_seconds"] = interval_minutes * 60
    return interval_df
