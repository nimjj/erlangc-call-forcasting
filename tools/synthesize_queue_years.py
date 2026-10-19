"""Generate synthetic prior years of Queue_All_Calls data from the real Oct 2025 - Sep 2026 year.

The real year is decomposed into daily totals
    level x weekday factor x annual factor x holiday factor x event factor x noise
with holidays and known one-off events masked before fitting. Each synthetic year
re-uses a smoothed, perturbed version of that seasonality, its own level curve
(FY24 flat, FY25 surge then easing into FY26), its own US holidays, and the real US
events of that year (see synthetic_events.json). Per-call fields are copied from
real calls with the same outcome and a similar hour, with jitter, synthetic caller
numbers, per-year agent rosters, and Call IDs consistent with Central time.

Only pandas, numpy and statsmodels are needed; the app package is not imported.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.interpolate import PchipInterpolator

ROOT = Path(__file__).resolve().parents[1]
REAL_PATH = ROOT / "testdata" / "New_year_data" / "Queue_All_Calls_2025-10_to_2026-09.csv"
OUT_DIR = ROOT / "testdata" / "Synthetic_years"
DATE_FORMAT = "%d/%b/%y %H:%M:%S"
TZ = "America/Chicago"
SLOT_MINUTES = 15
SLOTS = 1440 // SLOT_MINUTES
FOURIER_K = 3
HOUR_BUCKETS = [0, 8, 10, 12, 14, 16, 18, 24]
FAILED = ("EXITWITHTIMEOUT", "ABANDON")

# Load the shared holiday calendar by path: importing the app package pulls in ortools.
_spec = importlib.util.spec_from_file_location("holiday_calendar", ROOT / "app" / "core" / "forecasting" / "calendar.py")
holiday_calendar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(holiday_calendar)

# One-off spikes in the real year, masked so they are not copied into synthetic years.
REAL_EVENT_DAYS = ["2025-10-20", "2026-01-26", "2026-01-27", "2026-07-22"]

# Relative to the fitted real-year level (1.0 = real FY26 level at the same point of the season).
LEVEL_ANCHORS = [
    ("2023-10-01", 0.80), ("2024-04-01", 0.79), ("2024-09-30", 0.78),
    ("2025-01-15", 0.90), ("2025-05-15", 1.10), ("2025-08-01", 1.04), ("2025-09-30", 1.00),
]

YEARS = {
    "FY24": {
        "start": "2023-10-01", "end": "2024-09-30",
        "story": "Flat, slightly easing demand in a high-interest-rate economy.",
        "talk_drift": 0.97, "wait_scale": 0.92,
        # (agent, first month index, last month index, weight); month 0 = October.
        "roster": [
            ("Agent/2410", 0, 11, 1.0), ("Agent/2430", 0, 7, 0.8), ("Agent/2470", 0, 11, 0.9),
            ("Agent/2520", 3, 11, 0.7), ("Agent/2580", 0, 5, 0.6), ("Agent/2650", 0, 11, 1.0),
            ("Agent/2690", 0, 11, 0.9), ("Agent/2780", 6, 11, 0.8), ("Agent/2880", 0, 11, 0.3),
            ("Agent/2960", 0, 11, 1.2), ("Agent/2970", 0, 11, 1.2),
        ],
    },
    "FY25": {
        "start": "2024-10-01", "end": "2025-09-30",
        "story": "Post-election growth surge peaking in late spring 2025, then easing into FY26.",
        "talk_drift": 1.02, "wait_scale": 1.05,
        "roster": [
            ("Agent/2410", 0, 6, 0.8), ("Agent/2470", 0, 11, 0.9), ("Agent/2520", 0, 9, 0.7),
            ("Agent/2620", 0, 5, 0.6), ("Agent/2650", 0, 11, 1.0), ("Agent/2670", 4, 11, 0.4),
            ("Agent/2680", 2, 11, 0.5), ("Agent/2690", 0, 11, 0.9), ("Agent/2780", 0, 11, 0.9),
            ("Agent/2880", 0, 11, 0.3), ("Agent/2960", 0, 11, 1.2), ("Agent/2970", 0, 11, 1.2),
        ],
    },
}

DEFAULT_EVENTS = [
    # FY24
    {"date": "2024-01-16", "name": "Arctic blast across the central US", "multiplier": 1.3, "window": None, "timeout_surge": False, "note": "Record cold Jan 13-17, 2024; power and connectivity issues."},
    {"date": "2024-01-17", "name": "Arctic blast across the central US", "multiplier": 1.2, "window": None, "timeout_surge": False, "note": "Second day of elevated contacts."},
    {"date": "2024-02-22", "name": "AT&T nationwide wireless outage", "multiplier": 2.2, "window": [7, 13], "timeout_surge": True, "note": "Outage from ~3:30 am CT until early afternoon."},
    {"date": "2024-04-08", "name": "Total solar eclipse", "multiplier": 0.4, "window": [13, 15], "timeout_surge": False, "note": "Afternoon dip as the path of totality crossed Texas to Ohio."},
    {"date": "2024-07-08", "name": "Hurricane Beryl (Houston)", "multiplier": 1.5, "window": None, "timeout_surge": False, "note": "Landfall Jul 8; ~2.2M customers without power."},
    {"date": "2024-07-09", "name": "Hurricane Beryl (Houston)", "multiplier": 1.45, "window": None, "timeout_surge": False, "note": "Outage aftermath."},
    {"date": "2024-07-10", "name": "Hurricane Beryl (Houston)", "multiplier": 1.35, "window": None, "timeout_surge": False, "note": "Outage aftermath."},
    {"date": "2024-07-11", "name": "Hurricane Beryl (Houston)", "multiplier": 1.2, "window": None, "timeout_surge": False, "note": "Restoration continues."},
    {"date": "2024-07-12", "name": "Hurricane Beryl (Houston)", "multiplier": 1.1, "window": None, "timeout_surge": False, "note": "Restoration continues."},
    {"date": "2024-07-19", "name": "CrowdStrike / Microsoft global IT outage", "multiplier": 3.0, "window": [7, 12], "timeout_surge": True, "note": "Faulty Falcon update crashed Windows hosts from ~midnight CT."},
    {"date": "2024-07-22", "name": "CrowdStrike recovery Monday", "multiplier": 1.6, "window": None, "timeout_surge": False, "note": "Weekend backlog of affected users."},
    {"date": "2024-09-27", "name": "Hurricane Helene", "multiplier": 1.3, "window": None, "timeout_surge": False, "note": "Landfall Sep 26; widespread Southeast outages."},
    {"date": "2024-09-30", "name": "Hurricane Helene", "multiplier": 1.25, "window": None, "timeout_surge": False, "note": "Following Monday."},
    # FY25
    {"date": "2024-10-10", "name": "Hurricane Milton", "multiplier": 1.3, "window": None, "timeout_surge": False, "note": "Florida landfall Oct 9 evening."},
    {"date": "2024-10-11", "name": "Hurricane Milton", "multiplier": 1.2, "window": None, "timeout_surge": False, "note": "Outage aftermath."},
    {"date": "2024-11-05", "name": "US general election day", "multiplier": 0.9, "window": None, "timeout_surge": False, "note": "Slightly quieter day."},
    {"date": "2025-01-09", "name": "National day of mourning (President Carter)", "multiplier": 0.9, "window": None, "timeout_surge": False, "note": "Federal offices closed."},
    {"date": "2025-01-21", "name": "Gulf Coast snowstorm", "multiplier": 0.6, "window": None, "timeout_surge": False, "note": "Record snow in Houston/New Orleans; closures."},
    {"date": "2025-01-22", "name": "Gulf Coast snowstorm", "multiplier": 1.5, "window": None, "timeout_surge": False, "note": "Rebound and outage reports."},
    {"date": "2025-01-23", "name": "Gulf Coast snowstorm", "multiplier": 1.3, "window": None, "timeout_surge": False, "note": "Rebound continues."},
    {"date": "2025-04-03", "name": "Tariff announcement market turmoil", "multiplier": 1.1, "window": None, "timeout_surge": False, "note": "Billing and pricing questions."},
    {"date": "2025-04-04", "name": "Tariff announcement market turmoil", "multiplier": 1.1, "window": None, "timeout_surge": False, "note": "Billing and pricing questions."},
    {"date": "2025-06-12", "name": "Google Cloud outage", "multiplier": 1.5, "window": [12, 16], "timeout_surge": False, "note": "Global outage from ~12:50 pm CT for about three hours."},
    {"date": "2025-07-07", "name": "Texas Hill Country floods aftermath", "multiplier": 1.2, "window": None, "timeout_surge": False, "note": "Flooding from Jul 4, 2025."},
]


# ---------------------------------------------------------------- real data

def seconds(text: str) -> int:
    days, hours, minutes, secs = map(int, re.match(r"(\d+)d (\d+)h (\d+)m (\d+)s", text).groups())
    return days * 86400 + hours * 3600 + minutes * 60 + secs


def epoch_seconds(values) -> np.ndarray:
    """Unix seconds for tz-aware timestamps, independent of pandas' datetime resolution."""
    index = pd.DatetimeIndex(values).tz_convert("UTC").tz_localize(None)
    return ((index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy(dtype=np.int64)


def duration_text(total: int) -> str:
    total = max(int(total), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days:02d}d {hours:02d}h {minutes:02d}m {secs:02d}s"


def load_real(path: Path) -> tuple[list[str], pd.DataFrame]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    frame["dt"] = pd.to_datetime(frame["Date"], format=DATE_FORMAT)
    for column, short in (("Wait time", "wait"), ("Ringing time", "ring"), ("Talk time", "talk")):
        frame[short] = frame[column].map(seconds)
    frame["day"] = frame["dt"].dt.normalize()
    frame["hour"] = frame["dt"].dt.hour
    frame["bucket"] = np.digitize(frame["hour"], HOUR_BUCKETS[1:-1])
    return header, frame


def year_fraction(days: pd.DatetimeIndex) -> np.ndarray:
    lengths = np.where(days.is_leap_year, 366.0, 365.0)
    return (days.dayofyear.to_numpy() - 1) / lengths


def fourier(frac: np.ndarray, k: int = FOURIER_K) -> np.ndarray:
    angle = 2 * np.pi * frac[:, None] * np.arange(1, k + 1)[None, :]
    return np.hstack([np.sin(angle), np.cos(angle)])


@dataclass
class DailyModel:
    intercept: float
    weekday: np.ndarray        # log factors, Monday first, Monday = 0
    annual: np.ndarray         # Fourier coefficients (sin..., cos...)
    alpha: float               # negative-binomial dispersion
    holiday_ratio: dict        # holiday name -> observed ratio to the fitted mean

    def log_mean(self, days: pd.DatetimeIndex, shift_days: float = 0.0, amplitude: float = 1.0,
                 weekday: np.ndarray | None = None) -> np.ndarray:
        frac = year_fraction(days) + shift_days / 365.25
        weekday = self.weekday if weekday is None else weekday
        return self.intercept + weekday[days.dayofweek] + amplitude * fourier(frac) @ self.annual


def fit_daily_model(real: pd.DataFrame) -> tuple[DailyModel, pd.DataFrame]:
    days = pd.date_range(real["day"].min(), real["day"].max(), freq="D")
    counts = real.groupby("day").size().reindex(days, fill_value=0)
    holidays = holiday_calendar.us_holidays(days[0], days[-1])
    masked = days.isin(holidays.index) | days.isin(pd.to_datetime(REAL_EVENT_DAYS))

    weekday_dummies = np.eye(7)[days.dayofweek][:, 1:]
    design = np.hstack([np.ones((len(days), 1)), weekday_dummies, fourier(year_fraction(days))])
    keep = ~masked
    poisson = sm.GLM(counts.to_numpy()[keep], design[keep], family=sm.families.Poisson()).fit()
    mu = poisson.predict(design[keep])
    y = counts.to_numpy()[keep]
    # Weight by mu^2 so sparse weekend days do not inflate the dispersion estimate.
    alpha = float(max(np.sum((y - mu) ** 2 - y) / np.sum(mu ** 2), 0.005))
    nb = sm.GLM(y, design[keep], family=sm.families.NegativeBinomial(alpha=alpha)).fit()
    params = nb.params
    fitted = np.exp(design @ params)

    ratios = {name: float(counts[day] / fitted[days.get_loc(day)]) for day, name in holidays.items()}
    model = DailyModel(
        intercept=float(params[0]),
        weekday=np.concatenate([[0.0], params[1:7]]),
        annual=params[7:],
        alpha=alpha,
        holiday_ratio=ratios,
    )
    daily = pd.DataFrame({"count": counts.to_numpy(), "fitted": fitted, "masked": masked}, index=days)
    return model, daily


def intraday_profiles(real: pd.DataFrame, exclude: pd.DatetimeIndex) -> dict:
    usable = real.loc[~real["day"].isin(exclude)]
    slot = (usable["dt"].dt.hour * 60 + usable["dt"].dt.minute) // SLOT_MINUTES

    def smooth(values: np.ndarray, width: int) -> np.ndarray:
        kernel = np.hanning(width + 2)[1:-1]
        kernel /= kernel.sum()
        padded = np.concatenate([values[-width:], values, values[:width]])
        out = np.convolve(padded, kernel, mode="same")[width:-width]
        out = out + 0.002 * out.sum() / len(out)  # rare off-hours calls stay possible
        return out / out.sum()

    profiles = {}
    for weekday in range(7):
        counts = np.bincount(slot[usable["dt"].dt.dayofweek == weekday], minlength=SLOTS).astype(float)
        profiles[weekday] = smooth(counts, 3 if weekday < 5 else 7)
    holiday_slots = (real["dt"].dt.hour * 60 + real["dt"].dt.minute) // SLOT_MINUTES
    holiday_counts = np.bincount(holiday_slots[real["day"].isin(exclude)], minlength=SLOTS).astype(float)
    weekday_mean = np.mean([profiles[d] for d in range(5)], axis=0)
    profiles["holiday"] = 0.5 * smooth(holiday_counts, 7) + 0.5 * weekday_mean if holiday_counts.sum() else weekday_mean
    return profiles


# ---------------------------------------------------------------- synthesis

def level_curve(days: pd.DatetimeIndex) -> np.ndarray:
    anchor_days = pd.to_datetime([day for day, _ in LEVEL_ANCHORS])
    x = (anchor_days - anchor_days[0]).days.to_numpy(dtype=float)
    curve = PchipInterpolator(x, [value for _, value in LEVEL_ANCHORS], extrapolate=True)
    return curve((days - anchor_days[0]).days.to_numpy(dtype=float))


def month_wobble(days: pd.DatetimeIndex, rng: np.random.Generator) -> np.ndarray:
    months = pd.period_range(days[0], days[-1], freq="M")
    mids = pd.to_datetime([m.start_time + (m.end_time - m.start_time) / 2 for m in months])
    values = 1 + rng.uniform(-0.04, 0.04, len(months))
    return np.interp(days.asi8, mids.asi8, values)


def holiday_multiplier(name: str, observed: dict, rng: np.random.Generator) -> float:
    prior = holiday_calendar.DEFAULT_MULTIPLIERS[holiday_calendar.holiday_class(name)]
    learned = 0.7 * observed[name] + 0.3 * prior if name in observed else prior
    return float(max(learned * rng.uniform(0.85, 1.15), 0.0))


def negative_binomial(mean: np.ndarray, alpha: float, rng: np.random.Generator) -> np.ndarray:
    rate = mean * rng.gamma(1 / alpha, alpha, size=len(mean))
    return rng.poisson(rate)


def sample_times(day: pd.Timestamp, profile: np.ndarray, n: int, rng: np.random.Generator,
                 shift_minutes: float = 0.0, window: list | None = None) -> np.ndarray:
    if n <= 0:
        return np.array([], dtype="datetime64[ns]")
    weights = profile.copy()
    if window is not None:
        mask = np.zeros(SLOTS, dtype=bool)
        mask[window[0] * 60 // SLOT_MINUTES: window[1] * 60 // SLOT_MINUTES] = True
        weights = np.where(mask, weights + weights[mask].mean() * 0.2, 0.0)
    weights = weights / weights.sum()
    slots = rng.choice(SLOTS, size=n, p=weights)
    offsets = slots * SLOT_MINUTES * 60 + rng.integers(0, SLOT_MINUTES * 60, size=n) + int(shift_minutes * 60)
    offsets = np.clip(offsets, 0, 86399)
    return (day + pd.to_timedelta(np.sort(offsets), unit="s")).to_numpy()


def synthesize_arrivals(model: DailyModel, profiles: dict, label: str, spec: dict, events: list,
                        rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    days = pd.date_range(spec["start"], spec["end"], freq="D")
    params = {
        "annual_amplitude": float(rng.uniform(0.85, 1.15)),
        "annual_shift_days": float(rng.uniform(-10, 10)),
        "weekday_perturbation": rng.uniform(-0.03, 0.03, 7).round(4).tolist(),
        "intraday_shift_minutes": float(rng.uniform(-15, 15)),
    }
    weekday = model.weekday + np.log1p(np.array(params["weekday_perturbation"]))
    base = np.exp(model.log_mean(days, params["annual_shift_days"], params["annual_amplitude"], weekday))
    level = level_curve(days)
    wobble = month_wobble(days, rng)
    holidays = holiday_calendar.us_holidays(days[0], days[-1])
    holiday_mult = {day: holiday_multiplier(name, model.holiday_ratio, rng) for day, name in holidays.items()}

    by_day: dict[pd.Timestamp, list[dict]] = {}
    for event in events:
        if event.get("enabled", True):
            by_day.setdefault(pd.Timestamp(event["date"]).normalize(), []).append(event)

    normal_mean = base * level * wobble
    rows, daily_rows = [], []
    for index, day in enumerate(days):
        mean = normal_mean[index] * holiday_mult.get(day, 1.0)
        profile = profiles["holiday"] if day in holiday_mult and holiday_mult[day] < 0.9 else profiles[day.dayofweek]
        count = int(negative_binomial(np.array([mean]), model.alpha, rng)[0])
        times = sample_times(day, profile, count, rng, params["intraday_shift_minutes"])
        surge = np.zeros(len(times), dtype=bool)
        for event in by_day.get(day, []):
            multiplier, window = float(event["multiplier"]), event.get("window")
            if multiplier >= 1:
                extra = int(rng.poisson((multiplier - 1) * mean))
                extra_times = sample_times(day, profile, extra, rng, params["intraday_shift_minutes"], window)
                times = np.concatenate([times, extra_times])
                surge = np.concatenate([surge, np.full(extra, bool(event.get("timeout_surge")))])
            else:
                if window is None:
                    keep = rng.random(len(times)) < multiplier
                else:
                    hours = pd.DatetimeIndex(times).hour
                    in_window = (hours >= window[0]) & (hours < window[1])
                    keep = ~in_window | (rng.random(len(times)) < multiplier)
                times, surge = times[keep], surge[keep]
        load = len(times) / max(normal_mean[index], 1.0)
        rows.append(pd.DataFrame({"dt": times, "surge": surge, "load": load}))
        daily_rows.append({
            "date": day, "count": len(times), "normal_mean": normal_mean[index], "base_mean": base[index],
            "level": level[index], "holiday": holidays.get(day, ""),
            "event": "; ".join(e["name"] for e in by_day.get(day, [])),
        })
    arrivals = pd.concat(rows, ignore_index=True).sort_values("dt", kind="stable").reset_index(drop=True)
    params["holiday_multipliers"] = {str(day.date()): round(value, 3) for day, value in holiday_mult.items()}
    return arrivals, pd.DataFrame(daily_rows).set_index("date"), params


def assign_outcomes(arrivals: pd.DataFrame, base_mix: pd.Series, rng: np.random.Generator) -> np.ndarray:
    outcomes = base_mix.index.to_numpy()
    mix = rng.dirichlet(base_mix.to_numpy() * 5000)
    failed = np.isin(outcomes, FAILED)
    result = np.empty(len(arrivals), dtype=object)
    for load, group in arrivals.groupby(arrivals["load"].round(2)):
        probs = mix.copy()
        probs[failed] *= float(np.clip(load, 0.5, 3.0)) ** 1.5
        probs /= probs.sum()
        result[group.index] = rng.choice(outcomes, size=len(group), p=probs)
    surge_index = arrivals.index[arrivals["surge"]]
    if len(surge_index):
        surge_probs = np.where(outcomes == "EXITWITHTIMEOUT", 0.5, np.where(outcomes == "ABANDON", 0.1, 0.0))
        rest = ~failed
        surge_probs[rest] = 0.4 * mix[rest] / mix[rest].sum()
        result[surge_index] = rng.choice(outcomes, size=len(surge_index), p=surge_probs)
    return result


class CallerPool:
    """Synthetic caller numbers with the real repeat-caller structure and area-code mix."""

    def __init__(self, real: pd.DataFrame, rng: np.random.Generator):
        self.rng = rng
        ids = real["Caller ID"]
        digits = ids[ids.str.fullmatch(r"\d{10}|1\d{10}")]
        self.area_codes = digits.str[-10:].str[:3].value_counts(normalize=True)
        self.eleven_share = float((digits.str.len() == 11).mean())
        self.anonymous = ids[~ids.str.fullmatch(r"\+?\d+")].value_counts(normalize=True)
        self.anonymous_share = float((~ids.str.fullmatch(r"\+?\d+")).mean())
        self.calls_per_caller = ids[ids.str.fullmatch(r"\+?\d+")].value_counts().to_numpy()
        self.real_numbers = set(digits.str[-10:])
        self.numbers: list[str] = []
        self.weights = np.array([])

    def _new_number(self) -> str:
        while True:
            area = self.rng.choice(self.area_codes.index, p=self.area_codes.to_numpy())
            number = f"{area}{self.rng.integers(2, 10)}{self.rng.integers(0, 100):02d}{self.rng.integers(0, 10000):04d}"
            if number not in self.real_numbers:
                return number

    def refresh(self, calls: int, keep_share: float) -> None:
        """Start a new year's pool, keeping a share of the previous year's callers (busiest first)."""
        size = max(int(round(len(self.calls_per_caller) * calls / self.calls_per_caller.sum())), 1)
        kept = []
        if self.numbers:
            order = np.argsort(-self.weights)
            kept = [self.numbers[i] for i in order[: int(len(order) * keep_share)]]
        fresh = [self._new_number() for _ in range(max(size - len(kept), 0))]
        self.numbers = (kept + fresh)[:size]
        self.weights = self.rng.choice(self.calls_per_caller, size=len(self.numbers)).astype(float)
        self.weights *= self.rng.uniform(0.8, 1.2, len(self.numbers))
        self.eleven = self.rng.random(len(self.numbers)) < self.eleven_share

    def draw(self, n: int) -> np.ndarray:
        picks = self.rng.choice(len(self.numbers), size=n, p=self.weights / self.weights.sum())
        out = np.array([("1" + self.numbers[i]) if self.eleven[i] else self.numbers[i] for i in picks], dtype=object)
        anonymous = self.rng.random(n) < self.anonymous_share
        out[anonymous] = self.rng.choice(self.anonymous.index, size=int(anonymous.sum()), p=self.anonymous.to_numpy())
        return out


def build_rows(arrivals: pd.DataFrame, outcomes: np.ndarray, real: pd.DataFrame, spec: dict,
               callers: CallerPool, call_id_state: dict, rng: np.random.Generator) -> pd.DataFrame:
    n = len(arrivals)
    hours = pd.DatetimeIndex(arrivals["dt"]).hour
    buckets = np.digitize(hours, HOUR_BUCKETS[1:-1])
    templates = np.empty(n, dtype=np.int64)
    groups = {key: idx.to_numpy() for key, idx in real.groupby(["Ended", "bucket"]).groups.items()}
    by_outcome = {key: idx.to_numpy() for key, idx in real.groupby("Ended").groups.items()}
    for i in range(n):
        pool = groups.get((outcomes[i], buckets[i]))
        if pool is None or len(pool) < 5:
            pool = by_outcome[outcomes[i]]
        templates[i] = rng.choice(pool)
    template = real.loc[templates].reset_index(drop=True)

    jitter = lambda: rng.uniform(0.85, 1.15, n)  # noqa: E731
    load = arrivals["load"].to_numpy()
    talk = np.where(template["talk"] > 0, np.maximum(np.rint(template["talk"] * spec["talk_drift"] * jitter()), 1), 0)
    wait_factor = spec["wait_scale"] * np.clip(load, 0.7, 2.5) ** 0.5 * jitter()
    wait = np.where(template["Ended"] == "EXITWITHTIMEOUT", template["wait"], np.rint(template["wait"] * wait_factor))
    ring = np.rint(template["ring"] * jitter())

    month_index = ((pd.DatetimeIndex(arrivals["dt"]).year - pd.Timestamp(spec["start"]).year) * 12
                   + pd.DatetimeIndex(arrivals["dt"]).month - pd.Timestamp(spec["start"]).month).to_numpy()
    agents = np.empty(n, dtype=object)
    for month in np.unique(month_index):
        active = [(agent, weight) for agent, first, last, weight in spec["roster"] if first <= month <= last]
        names = np.array([agent for agent, _ in active])
        weights = np.array([weight for _, weight in active])
        idx = np.flatnonzero(month_index == month)
        agents[idx] = rng.choice(names, size=len(idx), p=weights / weights.sum())
    abandon_none = (outcomes == "ABANDON") & (rng.random(n) < (real.loc[real["Ended"] == "ABANDON", "Agent"] == "NONE").mean())
    agents[(outcomes == "EXITWITHTIMEOUT") | abandon_none] = "NONE"

    local = pd.DatetimeIndex(arrivals["dt"]).tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
    epochs = epoch_seconds(local) - rng.choice(call_id_state["offsets"], size=n)
    steps = rng.choice(call_id_state["steps"], size=n)
    sequences = np.empty(n, dtype=np.int64)
    current = call_id_state["sequence"]
    for i in range(n):
        current = int(rng.integers(1, 2000)) if rng.random() < 1 / 4000 else current + int(steps[i])
        sequences[i] = current
    call_id_state["sequence"] = current

    return pd.DataFrame({
        "Call ID": [f"{epoch}.{seq}" for epoch, seq in zip(epochs, sequences)],
        "Date": pd.DatetimeIndex(arrivals["dt"]).strftime(DATE_FORMAT),
        "Caller ID": callers.draw(n),
        "Queue": "InitSupport",
        "Agent": agents,
        "Wait time": [duration_text(v) for v in wait],
        "Ringing time": [duration_text(v) for v in ring],
        "Talk time": [duration_text(v) for v in talk],
        "Entry": template["Entry"].to_numpy(),
        "Exit": template["Exit"].to_numpy(),
        "Transferred": template["Transferred"].to_numpy(),
        "Dumped": template["Dumped"].to_numpy(),
        "Ended": outcomes,
    })


def write_csv(frame: pd.DataFrame, header: list[str], path: Path) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(frame[header].itertuples(index=False, name=None))
    path.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))


def call_id_parts(real: pd.DataFrame) -> dict:
    local = real["dt"].dt.tz_localize(TZ, ambiguous="NaT", nonexistent="NaT").dropna()
    parts = real.loc[local.index, "Call ID"].str.split(".", expand=True)
    offsets = pd.Series(epoch_seconds(local) - parts[0].astype(np.int64).to_numpy())
    parts = real["Call ID"].str.split(".", expand=True)
    steps = parts[1].astype(np.int64).diff()
    return {
        "offsets": offsets[offsets.between(0, 300)].astype(np.int64).to_numpy(),
        "steps": steps[(steps > 0) & (steps < 2000)].astype(np.int64).to_numpy(),
    }


# ---------------------------------------------------------------- validation

def profile_of(frame: pd.DataFrame) -> np.ndarray:
    dt = pd.to_datetime(frame["Date"], format=DATE_FORMAT)
    weekday = dt[dt.dt.dayofweek < 5]
    return np.bincount((weekday.dt.hour * 2 + weekday.dt.minute // 30).to_numpy(), minlength=48) / len(weekday)


def daily_counts(frame: pd.DataFrame, start: str, end: str) -> pd.Series:
    dt = pd.to_datetime(frame["Date"], format=DATE_FORMAT)
    return dt.dt.normalize().value_counts().reindex(pd.date_range(start, end), fill_value=0).sort_index()


def normalized_month_shape(counts: pd.Series, exclude: pd.DatetimeIndex, level: pd.Series | None) -> pd.Series:
    usable = counts[(counts.index.dayofweek < 5) & ~counts.index.isin(exclude)]
    if level is not None:
        usable = usable / level.reindex(usable.index)
    by_month = usable.groupby(usable.index.month).mean()
    return by_month / by_month.mean()


def within_month_cv(counts: pd.Series, exclude: pd.DatetimeIndex) -> float:
    usable = counts[(counts.index.dayofweek < 5) & ~counts.index.isin(exclude)]
    grouped = usable.groupby(usable.index.to_period("M"))
    return float((grouped.std() / grouped.mean()).mean())


def ks_distance(a: np.ndarray, b: np.ndarray) -> float:
    grid = np.union1d(a, b)
    cdf_a = np.searchsorted(np.sort(a), grid, side="right") / len(a)
    cdf_b = np.searchsorted(np.sort(b), grid, side="right") / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


def validate(label: str, frame: pd.DataFrame, daily: pd.DataFrame, spec: dict, real: pd.DataFrame,
             real_daily: pd.DataFrame, events: list) -> dict:
    real_counts = real_daily["count"]
    synth_counts = daily_counts(frame, spec["start"], spec["end"])
    real_exclude = real_daily.index[real_daily["masked"]]
    synth_exclude = pd.to_datetime(
        list(holiday_calendar.us_holidays(spec["start"], spec["end"]).index)
        + [e["date"] for e in events if e.get("enabled", True)]
    )

    def weekday_share(counts: pd.Series, exclude) -> pd.Series:
        usable = counts[~counts.index.isin(exclude)]
        means = usable.groupby(usable.index.dayofweek).mean()
        return means / means.mean()

    real_share, synth_share = weekday_share(real_counts, real_exclude), weekday_share(synth_counts, synth_exclude)
    weekday_error = (synth_share / real_share - 1)
    intraday_corr = float(np.corrcoef(profile_of(real), profile_of(frame))[0, 1])
    real_shape = normalized_month_shape(real_counts, real_exclude, None)
    synth_shape = normalized_month_shape(synth_counts, synth_exclude, daily["level"])
    annual_corr = float(np.corrcoef(real_shape.sort_index(), synth_shape.reindex(real_shape.index))[0, 1])
    real_cv, synth_cv = within_month_cv(real_counts, real_exclude), within_month_cv(synth_counts, synth_exclude)
    answered_real = real.loc[real["talk"] > 0, "talk"].to_numpy()
    synth_talk = frame["Talk time"].map(seconds).to_numpy()
    talk_ks = ks_distance(answered_real, synth_talk[synth_talk > 0])
    real_mix = real["Ended"].value_counts(normalize=True)
    synth_mix = frame["Ended"].value_counts(normalize=True).reindex(real_mix.index, fill_value=0)
    mix_gap = float((synth_mix - real_mix).abs().max() * 100)
    parsed = pd.to_datetime(frame["Date"], format=DATE_FORMAT, errors="coerce")

    checks = {
        # +-3% deliberate perturbation plus ~3.5% sampling error (1 sd) on ~50 days per weekday.
        "weekday_ratio_weekdays_max_error_percent": (round(float(weekday_error.iloc[:5].abs().max() * 100), 2), 8.0),
        "weekday_ratio_weekend_max_error_percent": (round(float(weekday_error.iloc[5:].abs().max() * 100), 2), 25.0),
        "intraday_profile_correlation": (round(intraday_corr, 4), 0.97),
        "annual_shape_correlation": (round(annual_corr, 4), (0.80, 0.97)),
        "within_month_cv_ratio": (round(synth_cv / real_cv, 3), (0.85, 1.15)),
        "talk_time_ks": (round(talk_ks, 4), 0.05),
        "outcome_mix_max_gap_pp": (round(mix_gap, 2), 2.0),
        "duplicate_call_ids": (int(frame["Call ID"].duplicated().sum()), 0),
        "unparsed_or_out_of_range_dates": (int(parsed.isna().sum() + (~parsed.between(spec["start"], pd.Timestamp(spec["end"]) + pd.Timedelta(days=1))).sum()), 0),
    }
    results = {}
    for name, (value, limit) in checks.items():
        if isinstance(limit, tuple):
            passed = limit[0] <= value <= limit[1]
        elif name == "intraday_profile_correlation":
            passed = value >= limit
        else:
            passed = value <= limit
        results[name] = {"value": value, "limit": limit, "passed": bool(passed)}
    answered = int((synth_talk >= 1).sum())
    results["app_keep_rate_percent"] = {
        "value": round(answered / len(frame) * 100, 2),
        "real": round(float((real["talk"] >= 1).mean() * 100), 2),
        "passed": True,
    }
    return results


def continuity_check(fy25_daily: pd.DataFrame, real_daily: pd.DataFrame) -> dict:
    tail = fy25_daily.loc[fy25_daily.index >= fy25_daily.index[-1] - pd.Timedelta(days=55)]
    tail = tail.loc[(tail.index.dayofweek < 5) & (tail["holiday"] == "") & (tail["event"] == "")]
    head = real_daily.loc[real_daily.index <= real_daily.index[0] + pd.Timedelta(days=55)]
    head = head.loc[(head.index.dayofweek < 5) & ~head["masked"]]
    synth_level = tail["count"].sum() / tail["base_mean"].sum()
    real_level = head["count"].sum() / head["fitted"].sum()
    gap = abs(synth_level / real_level - 1) * 100
    return {"value": round(float(gap), 2), "limit": 5.0, "passed": bool(gap <= 5.0),
            "fy25_last_8_weeks_level": round(float(synth_level), 4), "fy26_first_8_weeks_level": round(float(real_level), 4)}


# ---------------------------------------------------------------- driver

def generate(seed: int, real_path: Path = REAL_PATH, events: list | None = None) -> dict:
    """Return synthetic frames, daily tables, parameters and validation results (no files written)."""
    rng = np.random.default_rng(seed)
    header, real = load_real(real_path)
    model, real_daily = fit_daily_model(real)
    profiles = intraday_profiles(real, real_daily.index[real_daily["masked"]])
    events = DEFAULT_EVENTS if events is None else events
    base_mix = real.loc[~real["day"].isin(pd.to_datetime(REAL_EVENT_DAYS)), "Ended"].value_counts(normalize=True)
    callers = CallerPool(real, rng)
    call_id_state = call_id_parts(real)
    call_id_state["sequence"] = int(rng.integers(10_000, 150_000))

    out = {"header": header, "real": real, "real_daily": real_daily, "model": model, "years": {}}
    for label, spec in YEARS.items():
        year_events = [e for e in events if spec["start"] <= e["date"] <= spec["end"]]
        arrivals, daily, params = synthesize_arrivals(model, profiles, label, spec, year_events, rng)
        outcomes = assign_outcomes(arrivals, base_mix, rng)
        callers.refresh(len(arrivals), keep_share=0.35)
        frame = build_rows(arrivals, outcomes, real, spec, callers, call_id_state, rng)
        out["years"][label] = {
            "frame": frame, "daily": daily, "params": params, "events": year_events,
            "validation": validate(label, frame, daily, spec, real, real_daily, year_events),
        }
    out["continuity"] = continuity_check(out["years"]["FY25"]["daily"], real_daily)
    return out


def passes(result: dict) -> bool:
    return result["continuity"]["passed"] and all(
        check["passed"] for year in result["years"].values() for check in year["validation"].values()
    )


def first_passing(start: int = 1, attempts: int = 50, events: list | None = None,
                  real_path: Path = REAL_PATH, skip: int = 0) -> tuple[int, dict, list[int]]:
    """Rejection sampling: the first seed (after `skip` accepted ones) whose draw passes every gate."""
    rejected = []
    for seed in range(start, start + attempts):
        result = generate(seed, real_path, events)
        if passes(result):
            if skip == 0:
                return seed, result, rejected
            skip -= 1
        else:
            rejected.append(seed)
    raise SystemExit(f"No seed in {start}..{start + attempts - 1} passed every validation gate.")


def write_report(result: dict, seed: int, out_dir: Path, files: dict, rejected: list[int] | None = None) -> bool:
    model, real_daily = result["model"], result["real_daily"]
    all_passed = result["continuity"]["passed"]
    lines = [
        "# Synthetic Queue_All_Calls years", "",
        f"Seed `{seed}`" + (f" (seeds {', '.join(map(str, rejected))} were rejected by the validation gates)" if rejected else "")
        + ". Source: real `Queue_All_Calls_2025-10_to_2026-09.csv` "
        f"({int(real_daily['count'].sum())} calls). Generated by `tools/synthesize_queue_years.py`.", "",
        "## Files", "",
    ]
    lines += [f"- `{name}`: {rows:,} rows" for name, rows in files.items()]
    lines += ["", "## Fitted real-year model", "",
              f"- Negative-binomial dispersion alpha = {model.alpha:.4f}",
              "- Weekday factors (Mon..Sun): " + ", ".join(f"{v:.3f}" for v in np.exp(model.weekday)),
              f"- Masked real event days: {', '.join(REAL_EVENT_DAYS)}",
              "- Observed holiday ratios: " + ", ".join(f"{k} {v:.2f}" for k, v in model.holiday_ratio.items()), ""]
    summary = {"seed": seed, "files": files, "continuity": result["continuity"], "years": {}}
    for label, year in result["years"].items():
        spec, frame, daily = YEARS[label], year["frame"], year["daily"]
        monthly = daily.groupby(daily.index.to_period("M"))["count"].sum()
        lines += [f"## {label} ({spec['start']} to {spec['end']})", "", spec["story"], "",
                  f"- Calls: {len(frame):,}; level curve {daily['level'].iloc[0]:.2f} -> {daily['level'].max():.2f} (max) -> {daily['level'].iloc[-1]:.2f}",
                  f"- Annual amplitude x{year['params']['annual_amplitude']:.3f}, shift {year['params']['annual_shift_days']:+.1f} days, "
                  f"intraday shift {year['params']['intraday_shift_minutes']:+.1f} min",
                  "- Monthly calls: " + ", ".join(f"{p.strftime('%b %y')} {v}" for p, v in monthly.items()), "",
                  "| Check | Value | Limit | Pass |", "|---|---|---|---|"]
        for name, check in year["validation"].items():
            lines.append(f"| {name} | {check['value']} | {check.get('limit', check.get('real', ''))} | {'yes' if check['passed'] else 'NO'} |")
            all_passed &= check["passed"]
        lines += ["", "Events injected:", "", "| Date | Event | Multiplier | Window | Calls that day |", "|---|---|---|---|---|"]
        for event in year["events"]:
            day = pd.Timestamp(event["date"])
            lines.append(f"| {event['date']} | {event['name']} | {event['multiplier']} | {event.get('window') or 'all day'} | {int(daily.loc[day, 'count'])} |")
        lines += ["", "Holidays:", "", "| Date | Holiday | Calls |", "|---|---|---|"]
        for day, row in daily.loc[daily["holiday"] != ""].iterrows():
            lines.append(f"| {day.date()} | {row['holiday']} | {int(row['count'])} |")
        lines.append("")
        summary["years"][label] = {"calls": len(frame), "params": year["params"], "validation": year["validation"],
                                   "monthly_calls": {str(p): int(v) for p, v in monthly.items()}}
    c = result["continuity"]
    lines += ["## FY25 -> FY26 continuity", "",
              f"Seasonally adjusted level, last 8 weeks of FY25 vs first 8 weeks of real FY26: gap {c['value']}% "
              f"(limit {c['limit']}%) -> {'pass' if c['passed'] else 'FAIL'}", "",
              "## Caveat", "",
              "The synthetic seasonality is derived from the real FY26 year (smoothed with 3 Fourier harmonics and "
              "perturbed per year). Forecast scores on FY26 that rely on annual seasonality learned from these years "
              "are therefore an upper bound.", ""]
    (out_dir / "synthetic_report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "synthetic_report.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return all_passed


def plot_daily(result: dict, out_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(16, 5))
    for label, year in result["years"].items():
        counts = year["daily"]["count"]
        ax.plot(counts.index, counts.to_numpy(), lw=0.7, label=f"{label} synthetic")
    real = result["real_daily"]["count"]
    ax.plot(real.index, real.to_numpy(), lw=0.7, color="black", label="FY26 real")
    for label, year in result["years"].items():
        for event in year["events"]:
            if event["multiplier"] >= 2:
                ax.annotate(event["name"].split(" (")[0], (pd.Timestamp(event["date"]), year["daily"].loc[event["date"], "count"]),
                            fontsize=7, xytext=(4, 2), textcoords="offset points")
    ax.set_ylabel("Calls per day")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "daily_calls_3yr.png", dpi=110)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real", type=Path, default=REAL_PATH)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--seed", type=int, default=None,
                        help="Fixed seed; by default the first seed from 1 upward that passes every gate.")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    events_path = args.out_dir / "synthetic_events.json"
    if events_path.exists():
        events = json.loads(events_path.read_text(encoding="utf-8"))
    else:
        events = [dict(event, enabled=True) for event in DEFAULT_EVENTS]
        events_path.write_text(json.dumps(events, indent=2), encoding="utf-8")

    if args.seed is None:
        seed, result, rejected = first_passing(events=events, real_path=args.real)
    else:
        seed, result, rejected = args.seed, generate(args.seed, args.real, events), []
    header, files = result["header"], {}
    frames = []
    for label, year in result["years"].items():
        spec = YEARS[label]
        name = f"Queue_All_Calls_{spec['start'][:7]}_to_{spec['end'][:7]}_SYNTH.csv"
        write_csv(year["frame"], header, args.out_dir / name)
        files[name] = len(year["frame"])
        frames.append(year["frame"])
    real_rows = result["real"][header]
    combined = pd.concat(frames + [real_rows], ignore_index=True)
    if combined["Call ID"].duplicated().any():
        raise SystemExit("Duplicate Call IDs across synthetic and real years.")
    name = f"Queue_All_Calls_{YEARS['FY24']['start'][:7]}_to_2026-09_3yr.csv"
    write_csv(combined, header, args.out_dir / name)
    files[name] = len(combined)

    passed = write_report(result, seed, args.out_dir, files, rejected)
    plot_daily(result, args.out_dir)
    for file_name, rows in files.items():
        print(f"{file_name}: {rows:,} rows")
    print((args.out_dir / "synthetic_report.md").read_text(encoding="utf-8"))
    if not passed:
        raise SystemExit("One or more validation gates failed; see synthetic_report.md")


if __name__ == "__main__":
    main()
