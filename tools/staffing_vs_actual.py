"""Compare Erlang C required staffing (from CDRs) with actual staffing (from attendance).

Required agents come from the app's own pipeline (read_cdr_csv -> preprocess_cdr ->
interval aggregation -> compute_interval_staffing). Actual agents are the logged-in
agent-minutes per interval from tools/clean_attendance.py, divided by the interval length.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.ingestion import preprocess_cdr, read_cdr_csv  # noqa: E402
from app.core.queuing.staffing import compute_interval_staffing  # noqa: E402


def required_staffing(cdr_path: Path, start: pd.Timestamp, end: pd.Timestamp, args) -> pd.DataFrame:
    raw = read_cdr_csv(cdr_path)
    clean = preprocess_cdr(cdr_path, raw_frame=raw)
    clean = clean[(clean["call_datetime"] >= start) & (clean["call_datetime"] < end)]
    rule = f"{args.interval_minutes}min"
    index = pd.date_range(start, end, freq=rule, inclusive="left", name="interval_start")
    intervals = (
        clean.set_index("call_datetime")
        .resample(rule)
        .agg(calls=("duration_seconds", "size"), handle_seconds=("duration_seconds", "sum"))
        .reindex(index, fill_value=0)
    )
    intervals["aht_seconds"] = (intervals["handle_seconds"] / intervals["calls"].replace(0, np.nan)).fillna(0.0)

    # The app only models answered calls; unanswered ones are reported alongside as unmet demand.
    export = pd.read_csv(cdr_path, dtype=str)
    unanswered = export[export["Disposition"].str.casefold() == "unanswered"] if "Disposition" in export else export.iloc[0:0]
    unanswered_times = pd.to_datetime(unanswered["Date"], errors="coerce")
    intervals["unanswered"] = (
        unanswered_times.dt.floor(rule).value_counts().reindex(index, fill_value=0).astype(int)
    )

    staffing = compute_interval_staffing(
        intervals.rename(columns={"calls": "call_volume"}).assign(aht_seconds=lambda f: f["aht_seconds"].clip(lower=1)),
        interval_seconds=args.interval_minutes * 60,
        target_seconds=args.target_seconds,
        target_service_level=args.target_service_level,
        shrinkage=args.shrinkage,
        max_agents=1000,
    )
    staffing.index = intervals.index
    result = intervals.join(staffing[["traffic_erlangs", "raw_agents", "scheduled_agents", "occupancy"]])
    dropped = len(raw) - len(clean)
    return result, len(raw), len(clean), dropped


def actual_staffing(attendance_path: Path, start: pd.Timestamp, end: pd.Timestamp, interval_minutes: int) -> tuple[pd.Series, float]:
    shifts = pd.read_csv(attendance_path, parse_dates=["shift_start", "shift_end"])
    shifts = shifts[(shifts["shift_end"] > start) & (shifts["shift_start"] < end)]
    s = shifts["shift_start"].clip(lower=start)
    e = shifts["shift_end"].clip(upper=end)
    total_minutes = int((end - start) / pd.Timedelta(minutes=1))
    delta = np.zeros(total_minutes + 1)
    np.add.at(delta, ((s - start) / pd.Timedelta(minutes=1)).astype(int).to_numpy(), 1)
    np.add.at(delta, ((e - start) / pd.Timedelta(minutes=1)).astype(int).to_numpy(), -1)
    per_minute = np.cumsum(delta)[:total_minutes]
    per_interval = per_minute.reshape(-1, interval_minutes).mean(axis=1)
    index = pd.date_range(start, end, freq=f"{interval_minutes}min", inclusive="left", name="interval_start")
    clipped_hours = float(((e - s).dt.total_seconds() / 3600).sum())
    return pd.Series(per_interval, index=index, name="actual_staffed"), clipped_hours


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdr", type=Path, default=ROOT / "testdata" / "merged_cdr_all_2026-08.csv")
    parser.add_argument("--attendance", type=Path, default=ROOT / "testdata" / "Full_year_test" / "attendance_clean.csv")
    parser.add_argument("--month", default="2026-08")
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--target-seconds", type=float, default=20)
    parser.add_argument("--target-service-level", type=float, default=80)
    parser.add_argument("--shrinkage", type=float, default=30)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "testdata" / "analysis")
    args = parser.parse_args()

    start = pd.Timestamp(f"{args.month}-01")
    end = start + pd.offsets.MonthBegin(1)
    hours_per_interval = args.interval_minutes / 60

    required, raw_rows, clean_rows, dropped = required_staffing(args.cdr, start, end, args)
    actual, clipped_hours = actual_staffing(args.attendance, start, end, args.interval_minutes)
    df = required.join(actual)
    df["gap_vs_scheduled"] = df["actual_staffed"] - df["scheduled_agents"]
    df["gap_vs_raw"] = df["actual_staffed"] - df["raw_agents"]

    tag = start.strftime("%b%Y").lower()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    interval_cols = [
        "calls", "unanswered", "aht_seconds", "traffic_erlangs", "raw_agents",
        "scheduled_agents", "occupancy", "actual_staffed", "gap_vs_scheduled", "gap_vs_raw",
    ]
    df[interval_cols].round(3).to_csv(args.out_dir / f"{tag}_interval_required_vs_actual.csv")

    day = df.index.normalize()
    daily = pd.DataFrame(
        {
            "weekday": day.day_name()[~day.duplicated()],
            "calls": df.groupby(day)["calls"].sum(),
            "unanswered": df.groupby(day)["unanswered"].sum(),
            "raw_agent_hours": df.groupby(day)["raw_agents"].sum() * hours_per_interval,
            "scheduled_agent_hours": df.groupby(day)["scheduled_agents"].sum() * hours_per_interval,
            "actual_agent_hours": df.groupby(day)["actual_staffed"].sum() * hours_per_interval,
        }
    )
    daily.index.name = "date"
    daily["over_under_hours"] = daily["actual_agent_hours"] - daily["scheduled_agent_hours"]
    daily.round(2).to_csv(args.out_dir / f"{tag}_daily_summary.csv")

    profile = (
        df.assign(weekday=df.index.day_name(), slot=df.index.strftime("%H:%M"))
        .groupby(["weekday", "slot"])[["calls", "unanswered", "raw_agents", "scheduled_agents", "actual_staffed"]]
        .mean()
        .round(2)
    )
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    profile = profile.reindex(weekdays, level="weekday")
    profile.to_csv(args.out_dir / f"{tag}_weekday_slot_profile.csv")

    # --- terminal summary -------------------------------------------------
    busy = df[df["calls"] > 0]
    print(f"Month {args.month}  interval={args.interval_minutes}min  target={args.target_seconds:g}s/{args.target_service_level:g}%  shrinkage={args.shrinkage:g}%")
    print(f"CDR rows={raw_rows}  answered calls kept by app cleaning={clean_rows}  dropped={dropped}  unanswered={int(df['unanswered'].sum())}")
    print(f"Attendance hours in month={clipped_hours:.1f}  (interval sum check={df['actual_staffed'].sum() * hours_per_interval:.1f})")
    print()
    print(f"Agent-hours  required raw={daily['raw_agent_hours'].sum():.1f}  required scheduled={daily['scheduled_agent_hours'].sum():.1f}  actual={daily['actual_agent_hours'].sum():.1f}")
    print(f"Intervals with calls={len(busy)}  avg calls={busy['calls'].mean():.2f}  avg AHT={busy['handle_seconds'].sum() / busy['calls'].sum() / 60:.1f} min")
    print(f"  avg scheduled needed={busy['scheduled_agents'].mean():.2f}  avg actually on shift={busy['actual_staffed'].mean():.2f}")
    under = busy[busy["actual_staffed"] < busy["scheduled_agents"]]
    print(f"  understaffed intervals (actual < scheduled): {len(under)}  -> calls {int(under['calls'].sum())}, unanswered {int(under['unanswered'].sum())}")
    over = busy[busy["actual_staffed"] >= 2 * busy["scheduled_agents"]]
    print(f"  overstaffed >=2x intervals: {len(over)} of {len(busy)}")
    no_cover = df[(df["actual_staffed"] < 0.5) & ((df["calls"] + df["unanswered"]) > 0)]
    print(f"  intervals with calls but <0.5 agents on shift: {len(no_cover)}  (calls {int(no_cover['calls'].sum())}, unanswered {int(no_cover['unanswered'].sum())})")
    print()
    by_weekday = daily.groupby("weekday")[["calls", "unanswered", "scheduled_agent_hours", "actual_agent_hours"]].mean().reindex(weekdays)
    print("Average per weekday:")
    print(by_weekday.round(1).to_string())
    print()
    weekday_peak = profile.loc[["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]].groupby(level="slot").mean()
    weekday_peak = weekday_peak[(weekday_peak["calls"] > 0) | (weekday_peak["actual_staffed"] > 0.5)]
    print("Mon-Fri average by half hour (calls, unanswered, scheduled needed, actual on shift):")
    print(weekday_peak[["calls", "unanswered", "scheduled_agents", "actual_staffed"]].round(2).to_string())
    print(f"\nOutputs written to {args.out_dir}")


if __name__ == "__main__":
    main()
