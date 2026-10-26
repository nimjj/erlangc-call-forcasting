"""Daily-level forecast with annual seasonality and US holiday effects.

Used by build_stl_forecast once at least a year of history is available. Daily totals
are modelled on a log1p scale, so weekly, annual and holiday effects are multiplicative:

1. Robust weekly STL on daily totals, refitted after replacing holidays and outliers.
2. Annual Fourier curve fitted jointly with a linear spline trend (one knot per year)
   on the weekly-adjusted series, so seasonal dips are not mistaken for trend.
3. Damped linear trend over the recent deseasonalised series, with the level
   calibrated so the model reproduces the call total of the last four weeks.
4. Holiday multipliers learned from history, with class defaults for unseen holidays.
5. Daily totals split into intervals with recent weekday intraday profiles, rounded
   with largest remainders so interval counts add up to the daily total.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from statsmodels.tsa.seasonal import STL

from app.core.forecasting.calendar import DEFAULT_MULTIPLIERS, holiday_class, us_holidays

MIN_ANNUAL_HISTORY_DAYS = 365
TREND_DAMPING_PER_WEEK = 0.6
ANNUAL_HARMONICS = 3  # with 2+ years of history; 2 with less
OUTLIER_SIGMA = 4.0
OUTLIER_MIN_EXPECTED_CALLS = 5.0
# Seasonal smoother spans ~105 same-weekday observations; weekly factors average 13 weeks.
STL_SEASONAL_SMOOTHER = 105
WEEKLY_FACTOR_DAYS = 91
LEVEL_CALIBRATION_DAYS = 28
PROFILE_DAYS = 182
PROFILE_PRIOR_CALLS = 20.0
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _fourier(days: pd.DatetimeIndex, harmonics: int) -> np.ndarray:
    lengths = np.where(days.is_leap_year, 366.0, 365.0)
    frac = (days.dayofyear.to_numpy() - 1) / lengths
    angle = 2 * np.pi * frac[:, None] * np.arange(1, harmonics + 1)[None, :]
    return np.hstack([np.sin(angle), np.cos(angle)])


def _trend_basis(steps: np.ndarray, span_days: int) -> tuple[np.ndarray, np.ndarray]:
    # Yearly knots can bend the trend between years without absorbing the annual cycle.
    knots = np.arange(365, span_days - 180, 365, dtype=float)
    columns = [np.ones_like(steps), steps / 365] + [np.maximum(steps - knot, 0) / 365 for knot in knots]
    return np.column_stack(columns), knots


def _fill_same_weekday(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    filled = values.copy()
    for index in np.flatnonzero(mask):
        neighbours = np.arange(index - 21, index + 22, 7)
        neighbours = neighbours[(neighbours >= 0) & (neighbours < len(values)) & (neighbours != index)]
        neighbours = neighbours[~mask[neighbours]]
        filled[index] = np.median(values[neighbours]) if len(neighbours) else values[~mask].mean()
    return filled


def _profiles(call_series: pd.Series, intervals_per_day: int, excluded: pd.DatetimeIndex,
              holiday_days: pd.DatetimeIndex) -> tuple[dict[int, np.ndarray], np.ndarray]:
    frame = pd.DataFrame({"calls": call_series.to_numpy(dtype=float)}, index=call_series.index)
    frame["day"] = frame.index.normalize()
    frame["slot"] = np.tile(np.arange(intervals_per_day), len(frame) // intervals_per_day)

    def normalise(counts: np.ndarray, prior: np.ndarray) -> np.ndarray:
        smoothed = 0.5 * counts + 0.25 * np.roll(counts, 1) + 0.25 * np.roll(counts, -1)
        return (smoothed + PROFILE_PRIOR_CALLS * prior) / (smoothed.sum() + PROFILE_PRIOR_CALLS)

    recent = frame.loc[(frame["day"] >= frame["day"].max() - pd.Timedelta(days=PROFILE_DAYS - 1))
                       & ~frame["day"].isin(excluded)]
    pooled = recent.groupby("slot")["calls"].sum().reindex(range(intervals_per_day), fill_value=0).to_numpy()
    pooled = (pooled + 1e-9) / (pooled.sum() + 1e-9 * intervals_per_day)
    profiles = {}
    for weekday in range(7):
        counts = (recent.loc[recent.index.dayofweek == weekday].groupby("slot")["calls"].sum()
                  .reindex(range(intervals_per_day), fill_value=0).to_numpy())
        profiles[weekday] = normalise(counts, pooled)
    weekday_mean = np.mean([profiles[weekday] for weekday in range(5)], axis=0)
    holiday_counts = (frame.loc[frame["day"].isin(holiday_days)].groupby("slot")["calls"].sum()
                      .reindex(range(intervals_per_day), fill_value=0).to_numpy())
    return profiles, normalise(holiday_counts, weekday_mean)


def _allocate(daily_total: float, profile: np.ndarray) -> np.ndarray:
    total = int(round(daily_total))
    expected = total * profile
    counts = np.floor(expected).astype(np.int64)
    remainder = total - counts.sum()
    if remainder > 0:
        counts[np.argsort(-(expected - counts), kind="stable")[:remainder]] += 1
    return counts


def predict_annual(
    call_series: pd.Series,
    future_index: pd.DatetimeIndex,
    intervals_per_day: int,
    trend_lookback_days: int,
) -> tuple[np.ndarray, dict]:
    """Forecast interval call counts for future_index from a zero-filled interval history."""
    daily = call_series.resample("D").sum()
    days = daily.index
    if len(days) < MIN_ANNUAL_HISTORY_DAYS:
        raise ValueError(f"Annual seasonality needs at least {MIN_ANNUAL_HISTORY_DAYS} days of history.")
    future_days = pd.date_range(future_index[0].normalize(), future_index[-1].normalize(), freq="D")
    holidays = us_holidays(days[0], future_days[-1])

    log_calls = np.log1p(daily.to_numpy(dtype=float))
    is_holiday = days.isin(holidays.index)
    first_pass = STL(
        _fill_same_weekday(log_calls, is_holiday), period=7, seasonal=STL_SEASONAL_SMOOTHER, robust=True,
    ).fit()
    residual = first_pass.resid
    sigma = 1.4826 * np.median(np.abs(residual - np.median(residual)))
    # Log residuals on near-empty days (weekends) are noisy, so only busier days can be outliers.
    busy = np.expm1(first_pass.trend + first_pass.seasonal) >= OUTLIER_MIN_EXPECTED_CALLS
    outliers = (np.abs(residual) > OUTLIER_SIGMA * sigma) & busy & ~is_holiday
    masked = is_holiday | outliers
    clean = np.where(masked, first_pass.trend + first_pass.seasonal, log_calls)
    fit = STL(clean, period=7, seasonal=STL_SEASONAL_SMOOTHER, robust=True).fit()
    weekly = np.asarray(fit.seasonal)

    harmonics = ANNUAL_HARMONICS if len(days) >= 730 else 2
    steps = np.arange(len(days), dtype=float)
    trend_design, knots = _trend_basis(steps, len(days))
    fourier = _fourier(days, harmonics)
    adjusted = clean - weekly
    coefficients, *_ = np.linalg.lstsq(np.hstack([trend_design, fourier]), adjusted, rcond=None)
    annual_coefficients = coefficients[trend_design.shape[1]:]
    deseasonalised = adjusted - fourier @ annual_coefficients

    lookback = int(min(len(days), trend_lookback_days))
    x = np.arange(lookback, dtype=float) - (lookback - 1)
    slope, level = np.polyfit(x, deseasonalised[-lookback:], 1)
    phi = TREND_DAMPING_PER_WEEK ** (1 / 7)
    horizon = (future_days - days[-1]).days.to_numpy(dtype=float)
    future_trend = level + slope * phi * (1 - phi ** horizon) / (1 - phi)

    recent = min(WEEKLY_FACTOR_DAYS, len(days))
    weekly_by_day = pd.Series(weekly[-recent:]).groupby(days[-recent:].dayofweek.to_numpy()).mean()
    future_log = (
        future_trend
        + weekly_by_day.reindex(future_days.dayofweek).to_numpy()
        + _fourier(future_days, harmonics) @ annual_coefficients
    )
    # Back-transforming log1p values biases the level; rescale so the same components
    # reproduce the call total of the last training weeks (holidays and outliers excluded).
    usable = ~masked
    recent_days = min(LEVEL_CALIBRATION_DAYS, lookback)
    backcast = np.expm1(
        level + slope * x[-recent_days:] + weekly[-recent_days:] + (fourier @ annual_coefficients)[-recent_days:]
    )
    recent_usable = usable[-recent_days:]
    recent_calls = daily.to_numpy(dtype=float)[-recent_days:][recent_usable].sum()
    calibration = float(recent_calls / backcast[recent_usable].sum()) if backcast[recent_usable].sum() > 0 else 1.0
    future_daily = np.maximum(calibration * np.expm1(future_log), 0)

    # Duan smearing for the in-sample expectation used to learn holiday multipliers.
    smearing = float(np.mean(np.exp(np.asarray(fit.resid)[usable][-365:])))
    expected_history = np.exp(np.asarray(fit.trend) + weekly) * smearing - 1
    ratios: dict[str, list[float]] = {}
    for day, name in holidays.items():
        if day in days:
            expected = expected_history[days.get_loc(day)]
            if expected >= 1:
                ratios.setdefault(name, []).append(float(daily[day] / expected))
    multipliers = {}
    for name in set(holidays.values):
        prior = DEFAULT_MULTIPLIERS[holiday_class(name)]
        observed = ratios.get(name, [])
        weight = len(observed) / (len(observed) + 1)
        learned = float(np.median(observed)) if observed else prior
        multipliers[name] = float(np.clip(weight * learned + (1 - weight) * prior, 0, 3))
    future_holidays = holidays.reindex(future_days)
    holiday_factor = np.array([multipliers[name] if isinstance(name, str) else 1.0 for name in future_holidays])
    future_daily = future_daily * holiday_factor

    profiles, holiday_profile = _profiles(call_series, intervals_per_day, days[masked], days[is_holiday])
    by_day = {}
    for day, total, factor in zip(future_days, future_daily, holiday_factor):
        profile = holiday_profile if factor < 0.9 else profiles[day.dayofweek]
        by_day[day] = _allocate(total, profile)
    slots = ((future_index.hour * 60 + future_index.minute) // (1440 // intervals_per_day)).to_numpy()
    values = np.array([by_day[day][slot] for day, slot in zip(future_index.normalize(), slots)], dtype=np.int64)

    annual_curve = _fourier(pd.date_range("2001-01-01", periods=365, freq="D"), harmonics) @ annual_coefficients
    return values, {
        "model_variant": "daily_stl_annual",
        "annual_seasonality": True,
        "annual_harmonics": harmonics,
        "annual_peak_to_trough_ratio": round(float(np.exp(annual_curve.max() - annual_curve.min())), 4),
        "trend_knots_days": knots.astype(int).tolist(),
        "trend_lookback_days_used": lookback,
        "trend_slope_per_day_log": float(slope),
        "trend_damping_per_week": TREND_DAMPING_PER_WEEK,
        "terminal_level_calls_per_day": round(float(np.expm1(level)), 3),
        "level_calibration_factor": round(calibration, 4),
        "level_calibration_days": recent_days,
        "weekly_factors": {WEEKDAY_NAMES[d]: round(float(np.exp(v)), 4) for d, v in weekly_by_day.items()},
        "holiday_days_adjusted": int(is_holiday.sum()),
        "holiday_multipliers": {name: round(value, 3) for name, value in sorted(multipliers.items())},
        "outliers_masked": int(outliers.sum()),
        "outlier_dates": [day.date().isoformat() for day in days[outliers]][:25],
        "residual_std_log": round(float(np.std(np.asarray(fit.resid)[usable])), 4),
        "predicted_daily_mean": round(float(future_daily.mean()), 3),
    }
