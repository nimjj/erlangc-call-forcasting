"""Clean and merge the monthly attendance exports (see export_attendance_xls.ps1).

Coverage is derived from the actual First In / Last Out punches. Shift names are
kept for reference only: from 2025 onward many worked days are labelled "Rest Day".
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "testdata" / "Full_year_test"
MAX_SESSION_HOURS = 16.0
TRAIN_RANGE = ("2024-10-01", "2025-09-30")
TEST_RANGE = ("2025-10-01", "2026-09-30")


def load_raw(raw_dir: Path) -> pd.DataFrame:
    frames = [pd.read_csv(path, dtype=str, keep_default_na=False) for path in sorted(raw_dir.glob("*.csv"))]
    if not frames:
        raise SystemExit(f"No CSV files in {raw_dir}; run tools/export_attendance_xls.ps1 first.")
    return pd.concat(frames, ignore_index=True)


def parse_clock(date: pd.Series, clock: pd.Series) -> pd.Series:
    clock = clock.str.strip().where(lambda s: s.str.fullmatch(r"\d{1,2}:\d{2}"))
    return pd.to_datetime(date.dt.strftime("%Y-%m-%d") + " " + clock, format="%Y-%m-%d %H:%M", errors="coerce")


def clean(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.DataFrame(
        {
            "employee_id": raw["Employee Id"].str.strip(),
            "employee_name": raw["Employee Name"].str.split().str.join(" "),
            "date": pd.to_datetime(raw["Date"].str.strip(), format="%d-%b-%Y", errors="coerce"),
            "first_in_raw": raw["First In"].str.strip(),
            "last_out_raw": raw["Last Out"].str.strip(),
            "shift_name": raw["Shift name"].str.split().str.join(" "),
            "source_file": raw["Source File"],
        }
    )
    df["shift_start"] = parse_clock(df["date"], df["first_in_raw"])
    df["shift_end"] = parse_clock(df["date"], df["last_out_raw"])
    # A Last Out at or before First In means the shift ran past midnight.
    crossed = df["shift_end"].notna() & df["shift_start"].notna() & (df["shift_end"] <= df["shift_start"])
    df.loc[crossed, "shift_end"] += pd.Timedelta(days=1)
    df["crossed_midnight"] = crossed
    df["worked_hours"] = ((df["shift_end"] - df["shift_start"]).dt.total_seconds() / 3600).round(4)
    df["planned_rest_day"] = df["shift_name"].str.contains("Rest Day", case=False)

    reason = pd.Series("", index=df.index)
    reason[df["date"].isna()] = "bad_date"
    reason[(reason == "") & df["shift_start"].isna()] = "missing_first_in"
    reason[(reason == "") & df["shift_end"].isna()] = "missing_last_out"
    reason[(reason == "") & (df["worked_hours"] > MAX_SESSION_HOURS)] = f"session_over_{MAX_SESSION_HOURS:g}h"
    reason[(reason == "") & (df["worked_hours"] <= 0)] = "zero_length_session"
    df["drop_reason"] = reason

    columns = [
        "employee_id",
        "employee_name",
        "date",
        "shift_start",
        "shift_end",
        "worked_hours",
        "crossed_midnight",
        "shift_name",
        "planned_rest_day",
        "source_file",
    ]
    kept = df.loc[reason == "", columns].sort_values(["date", "employee_id"]).reset_index(drop=True)
    rejected = df.loc[reason != ""].sort_values(["date", "employee_id"]).reset_index(drop=True)
    return kept, rejected


def report(raw: pd.DataFrame, kept: pd.DataFrame, rejected: pd.DataFrame) -> None:
    month = lambda frame: pd.to_datetime(frame["Date"] if "Date" in frame else frame["date"], format="%d-%b-%Y").dt.to_period("M")
    summary = pd.DataFrame(
        {
            "raw": raw.groupby(month(raw)).size(),
            "kept": kept.groupby(kept["date"].dt.to_period("M")).size(),
        }
    ).fillna(0).astype(int)
    by_reason = rejected.groupby([rejected["date"].dt.to_period("M"), "drop_reason"]).size().unstack(fill_value=0)
    summary = summary.join(by_reason).fillna(0).astype(int)
    summary["days_covered"] = kept.groupby(kept["date"].dt.to_period("M"))["date"].nunique()
    summary["agents"] = kept.groupby(kept["date"].dt.to_period("M"))["employee_id"].nunique()
    summary["agent_hours"] = kept.groupby(kept["date"].dt.to_period("M"))["worked_hours"].sum().round(1)
    print(summary.to_string())
    print(
        f"\nTotal raw={len(raw)} kept={len(kept)} rejected={len(rejected)} "
        f"(check: {len(kept) + len(rejected) == len(raw)}); crossed midnight={int(kept['crossed_midnight'].sum())}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DATA_DIR / "raw_csv")
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    raw = load_raw(args.raw_dir)
    kept, rejected = clean(raw)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    kept.to_csv(args.out_dir / "attendance_clean.csv", index=False)
    rejected.to_csv(args.out_dir / "attendance_rejects.csv", index=False)
    for name, (start, end) in {"train": TRAIN_RANGE, "test": TEST_RANGE}.items():
        part = kept[kept["date"].between(start, end)]
        part.to_csv(args.out_dir / f"attendance_{name}_{start[:7]}_{end[:7]}.csv", index=False)
        print(f"{name}: {start} .. {end}  rows={len(part)}  days={part['date'].nunique()}")

    report(raw, kept, rejected)


if __name__ == "__main__":
    main()
