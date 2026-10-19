from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from app.core.forecasting.stl import build_stl_forecast
from app.core.ingestion.cleaner import preprocess_cdr
from app.core.ingestion.interval import build_cdr_intervals
from app.core.ingestion.reader import read_cdr_csv

DEFAULT_TRAIN_DAYS = 14
MIN_TRAIN_DAYS = 7
YEARLY_LAG_DAYS = 364
YOY_WINDOW_DAYS = 91


def _accuracy(actual: pd.Series, predicted: pd.Series, dates: pd.Series) -> dict:
    actual = actual.astype(float)
    predicted = predicted.astype(float)
    error = predicted - actual
    total_actual = float(actual.sum())
    total_predicted = float(predicted.sum())

    daily = pd.DataFrame({"date": dates, "actual": actual, "predicted": predicted}).groupby("date").sum()
    daily_error = (daily["predicted"] - daily["actual"]).abs()
    busy_days = daily["actual"] > 0

    def ratio(numerator: float, denominator: float) -> float | None:
        return round(numerator / denominator * 100, 2) if denominator else None

    return {
        "interval_mae": round(float(error.abs().mean()), 3),
        "interval_rmse": round(float(np.sqrt((error ** 2).mean())), 3),
        "interval_wape_percent": ratio(float(error.abs().sum()), total_actual),
        "total_actual_calls": int(total_actual),
        "total_predicted_calls": int(total_predicted),
        "bias_percent": ratio(total_predicted - total_actual, total_actual),
        "daily_mape_percent": (
            round(float((daily_error[busy_days] / daily.loc[busy_days, "actual"]).mean() * 100), 2)
            if busy_days.any() else None
        ),
        "daily_wape_percent": ratio(float(daily_error.sum()), float(daily["actual"].sum())),
    }


def run_backtest(
    file_paths: Sequence[str | Path],
    filenames: Sequence[str] | None = None,
    train_end: Optional[str | pd.Timestamp] = None,
    interval_minutes: int = 30,
    seasonal_period: int | None = None,
    trend_lookback_days: int = 90,
    target_seconds: float = 20,
    target_service_level: float = 80,
    shrinkage: float = 30,
    max_agents: int = 1000,
    annual_seasonality: str = "auto",
) -> dict:
    """Hold out records from train_end onward, forecast them from the earlier history, and score the result."""
    if not file_paths:
        raise ValueError("Upload at least one CDR dataset.")
    interval_minutes = int(interval_minutes)
    if interval_minutes <= 0 or 1440 % interval_minutes != 0:
        raise ValueError("interval_minutes must be a positive divisor of 1440.")

    clean = pd.concat(
        [preprocess_cdr(path, raw_frame=read_cdr_csv(path)) for path in file_paths],
        ignore_index=True,
    )
    if clean.empty:
        raise ValueError("No valid Answered records found in the uploaded datasets.")

    first_day = clean["call_datetime"].min().normalize()
    last_day = clean["call_datetime"].max().normalize()
    if train_end:
        try:
            cutoff = pd.Timestamp(train_end).normalize()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"train_end must be a date in YYYY-MM-DD format; got {train_end!r}.") from exc
    else:
        cutoff = first_day + pd.Timedelta(days=DEFAULT_TRAIN_DAYS)
    train = clean.loc[clean["call_datetime"] < cutoff]
    test = clean.loc[clean["call_datetime"] >= cutoff]
    if train.empty or test.empty:
        raise ValueError(
            f"Split date {cutoff.date()} must fall inside the data range "
            f"{first_day.date()} to {last_day.date()} with records on both sides."
        )

    last_train_day = train["call_datetime"].max().normalize()
    train_span_days = (last_train_day + pd.Timedelta(days=1) - first_day).days
    if train_span_days < MIN_TRAIN_DAYS:
        raise ValueError(
            f"Training history spans {train_span_days} days; at least {MIN_TRAIN_DAYS} are required "
            f"(and 14 for STL to apply)."
        )

    # The forecast starts the day after the last training call, which can precede the cutoff.
    forecast_start = last_train_day + pd.Timedelta(days=1)
    forecast_days = (last_day + pd.Timedelta(days=1) - forecast_start).days
    forecast, summary = build_stl_forecast(
        file_paths=file_paths,
        filenames=filenames,
        interval_minutes=interval_minutes,
        forecast_days=forecast_days,
        seasonal_period=seasonal_period,
        trend_lookback_days=trend_lookback_days,
        target_seconds=target_seconds,
        target_service_level=target_service_level,
        shrinkage=shrinkage,
        max_agents=max_agents,
        history_end=cutoff,
        annual_seasonality=annual_seasonality,
    )

    rule = f"{interval_minutes}min"
    actual = (
        build_cdr_intervals(clean, interval_minutes=interval_minutes)
        .set_index("call_datetime")["call_volume"]
    )

    # Seasonal-naive baseline: repeat the last full training week, slot for slot.
    week_start = forecast_start - pd.Timedelta(days=7)
    last_week = actual.reindex(
        pd.date_range(week_start, forecast_start, freq=rule, inclusive="left"), fill_value=0
    ).to_numpy()

    scored = forecast.loc[forecast["interval_start"] >= cutoff, ["interval_start", "date", "call_volume"]]
    scored = scored.rename(columns={"call_volume": "predicted"}).reset_index(drop=True)
    scored["actual"] = actual.reindex(scored["interval_start"], fill_value=0).to_numpy()
    offsets = ((scored["interval_start"] - forecast_start) / pd.Timedelta(rule)).astype(int)
    scored["baseline"] = last_week[offsets % len(last_week)]

    baselines = ["baseline"]
    metrics = {
        "model": _accuracy(scored["actual"], scored["predicted"], scored["date"]),
        "seasonal_naive": _accuracy(scored["actual"], scored["baseline"], scored["date"]),
    }
    # Yearly seasonal-naive: same weekday 52 weeks earlier (from training data only),
    # scaled by the year-over-year ratio of the last 13 training weeks.
    if train_span_days >= YEARLY_LAG_DAYS + 7:
        lag = pd.Timedelta(days=YEARLY_LAG_DAYS)
        window = pd.Timedelta(days=YOY_WINDOW_DAYS)
        recent = actual.loc[(actual.index >= forecast_start - window) & (actual.index < forecast_start)].sum()
        prior = actual.loc[(actual.index >= forecast_start - window - lag) & (actual.index < forecast_start - lag)].sum()
        ratio = float(np.clip(recent / prior, 0.5, 2.0)) if prior > 0 else 1.0
        years_back = np.ceil(((scored["interval_start"] - forecast_start) / lag).clip(lower=0) + 1e-9).astype(int).clip(lower=1)
        source = scored["interval_start"] - years_back * lag
        scored["baseline_yearly"] = np.rint(
            actual.reindex(source, fill_value=0).to_numpy() * ratio ** years_back.to_numpy()
        ).astype(int)
        baselines.append("baseline_yearly")
        metrics["seasonal_naive_yearly"] = _accuracy(scored["actual"], scored["baseline_yearly"], scored["date"])
        metrics["seasonal_naive_yearly"]["yoy_ratio"] = round(ratio, 4)

    daily = scored.groupby("date", as_index=False)[["actual", "predicted", *baselines]].sum()
    decomposition = summary["decomposition_summary"]
    return {
        "split": {
            "data_start": first_day.date().isoformat(),
            "data_end": last_day.date().isoformat(),
            "train_start": first_day.date().isoformat(),
            "train_end": (cutoff - pd.Timedelta(days=1)).date().isoformat(),
            "train_span_days": train_span_days,
            "train_calls": int(len(train)),
            "test_start": cutoff.date().isoformat(),
            "test_end": last_day.date().isoformat(),
            "test_days": int((last_day - cutoff).days + 1),
            "test_calls": int(len(test)),
        },
        "forecast_method": summary["method"],
        "forecast_logic": summary["logic"],
        "fallback_reason": decomposition.get("fallback_reason"),
        "decomposition_summary": decomposition,
        "interval_minutes": interval_minutes,
        "annual_seasonality": annual_seasonality,
        "metrics": metrics,
        "daily": [
            {
                "date": row["date"],
                "weekday": pd.Timestamp(row["date"]).day_name(),
                "actual": int(row["actual"]),
                "predicted": int(row["predicted"]),
                **{name: int(row[name]) for name in baselines},
            }
            for row in daily.to_dict(orient="records")
        ],
        "intervals": [
            {
                "interval_start": row["interval_start"].strftime("%Y-%m-%dT%H:%M:%S"),
                "actual": int(row["actual"]),
                "predicted": int(row["predicted"]),
                **{name: int(row[name]) for name in baselines},
            }
            for row in scored.to_dict(orient="records")
        ],
    }
