"""Check how well the staffing plan covers the agents each shift really needed.

For every day and shift, the agents needed by the real calls (same Erlang C rule) are compared with
the agents the plan schedules from a forecast trained only on earlier data. Runs the full-year test
(mirrored years -> real year) and rolling monthly folds, for the old and the new settings:

    python tools/shift_coverage_check.py
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.core.forecasting.annual as annual  # noqa: E402
from app.core.forecasting.backtest import run_backtest  # noqa: E402
from app.core.scheduling.shifts import build_shift_requirements  # noqa: E402
from multi_year_backtest import REAL_EVENT_DAYS, THREE_YEAR, Scorer, cut_file, month_starts  # noqa: E402

OLD_FORECAST = {"TREND_DAMPING_PER_WEEK": 0.9, "STL_SEASONAL_SMOOTHER": 27, "ANNUAL_HARMONICS": 4}
NEW_FORECAST = {name: getattr(annual, name) for name in OLD_FORECAST}
POINT = {"coverage_percent": None, "night_minimum_agents": 0, "weekend_minimum_agents": 0}
SCENARIOS = [
    ("today: old forecast, point staffing", OLD_FORECAST, POINT),
    ("tuned forecast, point staffing", NEW_FORECAST, POINT),
    ("tuned + 80% coverage", NEW_FORECAST, {**POINT, "coverage_percent": 80}),
    ("tuned + 80% + minimum 3 night/weekend", NEW_FORECAST, {**POINT, "coverage_percent": 80, "night_minimum_agents": 3, "weekend_minimum_agents": 3}),
]
SHIFT_NAMES = {"NIGHT": "night 00-08", "MORNING": "morning 08-16", "EVENING": "evening 16-24"}

warnings.filterwarnings("ignore", message="Could not infer format")


def shifts_for(result: dict, scorer: Scorer, cutoff: str, column: str, policy: dict) -> pd.DataFrame:
    """Required agents per day and shift, from the forecast (column 'predicted') or the real calls ('actual')."""
    intervals = pd.DataFrame(result["intervals"])
    intervals["interval_start"] = pd.to_datetime(intervals["interval_start"])
    intervals["slot"] = (intervals["interval_start"].dt.hour * 60 + intervals["interval_start"].dt.minute) // scorer.interval_minutes
    aht = scorer.slot_aht(cutoff)
    frame = pd.DataFrame({
        "interval_start": intervals["interval_start"],
        "call_volume": intervals[column],
        "aht_seconds": aht.reindex(intervals["slot"]).to_numpy(),
        "scheduled_agents": scorer.staffing(intervals, column, aht),
    })
    return build_shift_requirements(frame, **policy).set_index(["date", "shift_code"])["required_agents"]


def evaluate(source: Path, fold: tuple[str, str] | None, scorer: Scorer, temp_dir: Path, interval_minutes: int) -> dict[str, pd.DataFrame]:
    """Per scenario, a frame of needed vs planned agents for each day and shift of the test window."""
    train_end, test_end = fold if fold else ("2025-10-01", None)
    path = cut_file(source, test_end, temp_dir)
    needed, out = None, {}
    for name, forecast_settings, policy in SCENARIOS:
        for setting, value in forecast_settings.items():
            setattr(annual, setting, value)
        result = run_backtest([path], train_end=train_end, interval_minutes=interval_minutes, annual_seasonality="auto")
        if needed is None:
            needed = shifts_for(result, scorer, train_end, "actual", POINT)
        planned = shifts_for(result, scorer, train_end, "predicted", policy)
        out[name] = pd.DataFrame({"needed": needed, "planned": planned})
    for setting, value in NEW_FORECAST.items():
        setattr(annual, setting, value)
    return out


def summarise(frame: pd.DataFrame) -> dict:
    busy = frame["needed"] > 0
    gap = (frame["planned"] - frame["needed"])
    return {
        "shifts": int(busy.sum()),
        "covered": 100 * float((gap[busy] >= 0).mean()),
        "short": int((-gap[busy].clip(upper=0)).sum()),
        "extra_busy": int(gap[busy].clip(lower=0).sum()),
        "extra_empty": int(frame.loc[~busy, "planned"].sum()),
        "needed": int(frame.loc[busy, "needed"].sum()),
    }


def line(label: str, frame: pd.DataFrame) -> str:
    s = summarise(frame)
    extra = s["extra_busy"] + s["extra_empty"]
    return (f"{label:42s} covered {s['covered']:5.1f}%  short {s['short']:4d}  extra on busy shifts {s['extra_busy']:4d}  "
            f"on empty shifts {s['extra_empty']:4d}  (total extra {100 * extra / s['needed']:+5.1f}% of {s['needed']} needed)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=THREE_YEAR, help="mirrored years + real year in one CDR file")
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--skip-folds", action="store_true", help="only run the full-year test")
    args = parser.parse_args()

    scorer = Scorer(args.data, args.interval_minutes, REAL_EVENT_DAYS)
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        full = evaluate(args.data, None, scorer, temp_dir, args.interval_minutes)
        print("Full year: trained on the mirrored years (Oct 2023 - Sep 2025), planned for the real year (Oct 2025 - Sep 2026)")
        for name, _, _ in SCENARIOS:
            print(line(name, full[name]))

        final = SCENARIOS[-1][0]
        for name in (SCENARIOS[0][0], final):
            frame = full[name]
            dates = frame.index.get_level_values("date")
            codes = frame.index.get_level_values("shift_code")
            print(f"\n{name}")
            for code, label in SHIFT_NAMES.items():
                print("  " + line(label, frame[codes == code]))
            print("  " + line("weekdays", frame[dates.dayofweek < 5]))
            print("  " + line("weekends", frame[dates.dayofweek >= 5]))

        if args.skip_folds:
            return
        folds = month_starts("2025-10-01", 12, 1)
        pooled = {name: [] for name, _, _ in SCENARIOS}
        per_fold = []
        for fold in folds:
            result = evaluate(args.data, fold, scorer, temp_dir, args.interval_minutes)
            window = lambda frame: frame[(frame.index.get_level_values("date") >= fold[0]) & (frame.index.get_level_values("date") < fold[1])]
            for name in pooled:
                pooled[name].append(window(result[name]))
            per_fold.append((fold[0][:7], summarise(window(result[SCENARIOS[0][0]]))["covered"], summarise(window(result[final]))["covered"]))
        print("\nRolling monthly folds (each month planned from data before it), pooled")
        for name, _, _ in SCENARIOS:
            print(line(name, pd.concat(pooled[name])))
        print("\nShifts covered by month: today -> new")
        print("  " + "  ".join(f"{month} {old:4.0f}->{new:4.0f}" for month, old, new in per_fold))
        print(f"  months where the new plan covers fewer shifts than today: {sum(new < old for _, old, new in per_fold)} of {len(per_fold)}")


if __name__ == "__main__":
    main()
