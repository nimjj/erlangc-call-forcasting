"""Multi-year backtest of the forecaster on synthetic FY24/FY25 + real FY26 call data.

Runs inside the app's Docker image (the host blocks ortools):

    docker compose run --rm -v ./tools:/app/tools:ro -v ./testdata:/app/testdata \
        app python tools/multi_year_backtest.py

Every test is scored for the current interval STL (annual_seasonality=off), the new
annual model (auto), and the weekly and yearly seasonal-naive baselines.

T1  FY24+FY25 synthetic -> FY26 real (365 days)
T2  rolling monthly origins over FY26 (12 folds)
T3  rolling quarterly origins over FY26 (4 folds)
T4  real Oct 2025 - Mar 2026 -> Apr - Sep 2026 (the earlier 6->6 test)
T5  FY24 synthetic -> FY25 synthetic (known ground truth)
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.forecasting.backtest import run_backtest  # noqa: E402
from app.core.forecasting.calendar import us_holidays  # noqa: E402
from app.core.ingestion import preprocess_cdr, read_cdr_csv  # noqa: E402
from app.core.queuing.staffing import compute_interval_staffing  # noqa: E402

SYNTH_DIR = ROOT / "testdata" / "Synthetic_years"
THREE_YEAR = SYNTH_DIR / "Queue_All_Calls_2023-10_to_2026-09_3yr.csv"
REAL = ROOT / "testdata" / "New_year_data" / "Queue_All_Calls_2025-10_to_2026-09.csv"
DATE_FORMAT = "%d/%b/%y %H:%M:%S"
REAL_EVENT_DAYS = ["2025-10-20", "2026-01-26", "2026-01-27", "2026-07-22"]
MODES = ("off", "auto")
MODEL_LABELS = {"off": "current STL", "auto": "annual STL", "baseline": "weekly naive", "baseline_yearly": "yearly naive"}

warnings.filterwarnings("ignore", message="Could not infer format")


def month_starts(start: str, count: int, step: int) -> list[tuple[str, str]]:
    starts = pd.date_range(start, periods=count + 1, freq=f"{step}MS")
    return [(a.date().isoformat(), b.date().isoformat()) for a, b in zip(starts[:-1], starts[1:])]


def cut_file(source: Path, end: str | None, temp_dir: Path, start: str | None = None) -> Path:
    """Copy rows with start <= Date < end into a temp CSV (same format as the source)."""
    if end is None and start is None:
        return source
    frame = pd.read_csv(source, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    dates = pd.to_datetime(frame["Date"], format=DATE_FORMAT)
    keep = np.ones(len(frame), dtype=bool)
    if end is not None:
        keep &= dates < pd.Timestamp(end)
    if start is not None:
        keep &= dates >= pd.Timestamp(start)
    path = temp_dir / f"{source.stem}_{start or 'begin'}_{end or 'end'}.csv"
    frame.loc[keep].to_csv(path, index=False, encoding="utf-8-sig")
    return path


class Scorer:
    """Staffing and calendar-aware scores on top of run_backtest's interval output."""

    def __init__(self, source: Path, interval_minutes: int, event_days: list[str]):
        clean = preprocess_cdr(source, raw_frame=read_cdr_csv(source))
        slot = (clean["call_datetime"].dt.hour * 60 + clean["call_datetime"].dt.minute) // interval_minutes
        self.clean = clean.assign(slot=slot)
        self.interval_minutes = interval_minutes
        self.event_days = set(event_days)

    def slot_aht(self, cutoff: str) -> pd.Series:
        train = self.clean.loc[self.clean["call_datetime"] < pd.Timestamp(cutoff)]
        global_aht = train["duration_seconds"].mean()
        aht = train.groupby("slot")["duration_seconds"].mean()
        return aht.reindex(range(1440 // self.interval_minutes)).fillna(global_aht)

    def staffing(self, intervals: pd.DataFrame, column: str, aht: pd.Series) -> np.ndarray:
        frame = pd.DataFrame({
            "call_volume": intervals[column].to_numpy(),
            "aht_seconds": aht.reindex(intervals["slot"]).to_numpy(),
        })
        metrics = compute_interval_staffing(
            frame, interval_seconds=self.interval_minutes * 60, target_seconds=20,
            target_service_level=80, shrinkage=30, max_agents=1000,
        )
        return metrics["scheduled_agents"].to_numpy()

    def score(self, result: dict, cutoff: str, columns: list[str]) -> dict:
        intervals = pd.DataFrame(result["intervals"])
        intervals["interval_start"] = pd.to_datetime(intervals["interval_start"])
        intervals["slot"] = (intervals["interval_start"].dt.hour * 60 + intervals["interval_start"].dt.minute) // self.interval_minutes
        aht = self.slot_aht(cutoff)
        required = self.staffing(intervals, "actual", aht)
        hours = self.interval_minutes / 60
        daily = pd.DataFrame(result["daily"]).set_index("date")
        daily.index = pd.to_datetime(daily.index)
        holidays = set(us_holidays(daily.index[0], daily.index[-1]).index)
        special = {"holiday": daily.index.isin(holidays), "event": daily.index.isin(pd.to_datetime(sorted(self.event_days)))}
        months = daily.groupby(daily.index.to_period("M")).sum()
        out = {}
        for column in columns:
            scheduled = self.staffing(intervals, column, aht)
            gap = scheduled - required
            month_error = (months[column] / months["actual"].replace(0, np.nan) - 1) * 100
            entry = {
                "staffing_understaffed_interval_percent": round(float((gap < 0).mean() * 100), 2),
                "staffing_overstaffed_interval_percent": round(float((gap > 0).mean() * 100), 2),
                "staffing_net_agent_hours": round(float(gap.sum() * hours), 1),
                "staffing_abs_agent_hours": round(float(np.abs(gap).sum() * hours), 1),
                "monthly_abs_error_percent_mean": round(float(month_error.abs().mean()), 2),
                "monthly_abs_error_percent_max": round(float(month_error.abs().max()), 2),
            }
            for name, mask in special.items():
                if mask.any():
                    error = (daily.loc[mask, column] - daily.loc[mask, "actual"]).abs().sum()
                    entry[f"{name}_day_wape_percent"] = round(float(error / max(daily.loc[mask, "actual"].sum(), 1) * 100), 2)
                    entry[f"{name}_days"] = int(mask.sum())
            out[column] = entry
        return out


def run_case(name: str, source: Path, train_end: str, test_end: str | None, temp_dir: Path,
             scorer: Scorer, interval_minutes: int, train_start: str | None = None) -> dict:
    path = cut_file(source, test_end, temp_dir, train_start)
    runs, daily = {}, None
    for mode in MODES:
        started = time.time()
        result = run_backtest([path], train_end=train_end, interval_minutes=interval_minutes, annual_seasonality=mode)
        runs[mode] = result
        frame = pd.DataFrame(result["daily"]).set_index("date")
        if daily is None:
            daily = frame.drop(columns=["weekday", "predicted"])
        daily[f"predicted_{mode}"] = frame["predicted"]
        print(f"  {name} [{mode}] {result['decomposition_summary'].get('model_variant')} "
              f"daily WAPE {result['metrics']['model']['daily_wape_percent']}% ({time.time() - started:.0f}s)", flush=True)

    reference = runs["auto"]
    metrics = {mode: runs[mode]["metrics"]["model"] for mode in MODES}
    metrics["baseline"] = reference["metrics"]["seasonal_naive"]
    if "seasonal_naive_yearly" in reference["metrics"]:
        metrics["baseline_yearly"] = reference["metrics"]["seasonal_naive_yearly"]
    intervals = pd.DataFrame(reference["intervals"])
    intervals["predicted_off"] = [row["predicted"] for row in runs["off"]["intervals"]]
    intervals["predicted_auto"] = intervals.pop("predicted")
    reference = dict(reference, intervals=intervals.to_dict(orient="records"),
                     daily=daily.reset_index().to_dict(orient="records"))
    columns = ["predicted_off", "predicted_auto", "baseline"] + (["baseline_yearly"] if "baseline_yearly" in metrics else [])
    extra = scorer.score(reference, reference["split"]["test_start"], columns)
    for key, column in (("off", "predicted_off"), ("auto", "predicted_auto"), ("baseline", "baseline"), ("baseline_yearly", "baseline_yearly")):
        if key in metrics:
            metrics[key] = {**metrics[key], **extra[column]}
    return {
        "name": name, "split": reference["split"], "metrics": metrics,
        "decomposition": {mode: runs[mode]["decomposition_summary"] for mode in MODES},
        "daily": daily.reset_index(),
    }


def fold_summary(cases: list[dict]) -> dict:
    """Pool folds: WAPE recomputed from summed errors, other metrics averaged."""
    pooled = {}
    for key in cases[0]["metrics"]:
        frames = [case["daily"] for case in cases if key in case["metrics"]]
        column = {"off": "predicted_off", "auto": "predicted_auto"}.get(key, key)
        daily = pd.concat(frames)
        error = (daily[column] - daily["actual"]).abs().sum()
        values = [case["metrics"][key] for case in cases if key in case["metrics"]]
        pooled[key] = {
            "daily_wape_percent": round(float(error / daily["actual"].sum() * 100), 2),
            "bias_percent": round(float((daily[column].sum() / daily["actual"].sum() - 1) * 100), 2),
            "fold_daily_wape_mean": round(float(np.mean([v["daily_wape_percent"] for v in values])), 2),
            "interval_wape_percent_mean": round(float(np.mean([v["interval_wape_percent"] for v in values])), 2),
            "staffing_understaffed_interval_percent": round(float(np.mean([v["staffing_understaffed_interval_percent"] for v in values])), 2),
            "staffing_abs_agent_hours": round(float(np.sum([v["staffing_abs_agent_hours"] for v in values])), 1),
            "staffing_net_agent_hours": round(float(np.sum([v["staffing_net_agent_hours"] for v in values])), 1),
            "folds": len(values),
        }
    return pooled


def run_suite(three_year: Path, real: Path, temp_dir: Path, interval_minutes: int, event_days: list[str],
              tests: set[str]) -> dict:
    scorer3 = Scorer(three_year, interval_minutes, event_days)
    results = {}
    if "T1" in tests:
        print("T1 FY24+FY25 synthetic -> FY26 real", flush=True)
        results["T1"] = run_case("T1", three_year, "2025-10-01", None, temp_dir, scorer3, interval_minutes)
    for test, step, count in (("T2", 1, 12), ("T3", 3, 4)):
        if test in tests:
            print(f"{test} rolling {'monthly' if step == 1 else 'quarterly'} origins over FY26", flush=True)
            folds = [run_case(f"{test} {start}", three_year, start, end, temp_dir, scorer3, interval_minutes)
                     for start, end in month_starts("2025-10-01", count, step)]
            results[test] = {"folds": folds, "pooled": fold_summary(folds)}
    if "T4" in tests:
        print("T4 real Oct-Mar -> Apr-Sep (earlier 6->6 test)", flush=True)
        results["T4"] = run_case("T4", real, "2026-04-01", None, temp_dir, Scorer(real, interval_minutes, event_days), interval_minutes)
    if "T5" in tests:
        print("T5 FY24 synthetic -> FY25 synthetic", flush=True)
        results["T5"] = run_case("T5", three_year, "2024-10-01", "2025-10-01", temp_dir, scorer3, interval_minutes)
    return results


def headline(results: dict) -> dict:
    rows = {}
    for test, result in results.items():
        metrics = result["pooled"] if "pooled" in result else result["metrics"]
        rows[test] = {key: {"daily_wape_percent": value["daily_wape_percent"], "bias_percent": value["bias_percent"]}
                      for key, value in metrics.items()}
    return rows


def write_outputs(results: dict, robustness: dict, out_dir: Path, seed_note: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for test, result in results.items():
        cases = result["folds"] if "folds" in result else [result]
        pd.concat([case["daily"].assign(test=case["name"]) for case in cases]).to_csv(out_dir / f"{test}_daily.csv", index=False)

    def table(metrics: dict, keys: list[str]) -> list[str]:
        models = [m for m in ("off", "auto", "baseline", "baseline_yearly") if m in metrics]
        lines = ["| Metric | " + " | ".join(MODEL_LABELS[m] for m in models) + " |", "|---|" + "---|" * len(models)]
        for key in keys:
            if any(key in metrics[m] for m in models):
                lines.append(f"| {key} | " + " | ".join(str(metrics[m].get(key, "")) for m in models) + " |")
        return lines

    single_keys = ["daily_wape_percent", "daily_mape_percent", "bias_percent", "interval_wape_percent",
                   "monthly_abs_error_percent_mean", "monthly_abs_error_percent_max",
                   "holiday_day_wape_percent", "event_day_wape_percent",
                   "staffing_understaffed_interval_percent", "staffing_overstaffed_interval_percent",
                   "staffing_net_agent_hours", "staffing_abs_agent_hours", "total_actual_calls", "total_predicted_calls"]
    pooled_keys = ["daily_wape_percent", "bias_percent", "fold_daily_wape_mean", "interval_wape_percent_mean",
                   "staffing_understaffed_interval_percent", "staffing_net_agent_hours", "staffing_abs_agent_hours", "folds"]
    titles = {"T1": "T1 - FY24+FY25 synthetic -> FY26 real (365 days)", "T2": "T2 - rolling monthly origins over FY26",
              "T3": "T3 - rolling quarterly origins over FY26", "T4": "T4 - real Oct 2025-Mar 2026 -> Apr-Sep 2026 (earlier 6->6 test)",
              "T5": "T5 - FY24 synthetic -> FY25 synthetic"}
    lines = ["# Multi-year backtest", "", seed_note, "",
             "Models: **current STL** = interval-level weekly STL (`annual_seasonality=off`); **annual STL** = daily STL with "
             "annual seasonality and US holidays (`auto`); **weekly naive** = repeat the last training week; **yearly naive** = "
             "same weekday 52 weeks earlier scaled by the year-over-year ratio. WAPE = sum|error| / sum(actual). "
             "Staffing compares Erlang C scheduled agents from each forecast with agents required by the actual intervals "
             "(same slot AHT, 20 s / 80%, 30% shrinkage).", ""]
    for test, result in results.items():
        lines += [f"## {titles[test]}", ""]
        if "pooled" in result:
            lines += table(result["pooled"], pooled_keys) + ["", "Per fold (daily WAPE %):", "",
                                                             "| Fold | Test window | current STL | annual STL | weekly naive | yearly naive |", "|---|---|---|---|---|---|"]
            for case in result["folds"]:
                m = case["metrics"]
                lines.append(f"| {case['name']} | {case['split']['test_start']} to {case['split']['test_end']} | "
                             f"{m['off']['daily_wape_percent']} | {m['auto']['daily_wape_percent']} | {m['baseline']['daily_wape_percent']} | "
                             f"{m.get('baseline_yearly', {}).get('daily_wape_percent', '')} |")
        else:
            lines += [f"Train {result['split']['train_start']} to {result['split']['train_end']} "
                      f"({result['split']['train_calls']:,} calls); test {result['split']['test_start']} to {result['split']['test_end']} "
                      f"({result['split']['test_calls']:,} calls).", ""]
            lines += table(result["metrics"], single_keys)
            daily = result["daily"]
            daily["month"] = pd.to_datetime(daily["date"]).dt.strftime("%Y-%m")
            cols = [c for c in ("actual", "predicted_off", "predicted_auto", "baseline", "baseline_yearly") if c in daily]
            monthly = daily.groupby("month")[cols].sum()
            lines += ["", "Monthly totals:", "", "| Month | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
            lines += [f"| {month} | " + " | ".join(str(int(v)) for v in row) + " |" for month, row in monthly.iterrows()]
            auto = result["decomposition"]["auto"]
            if auto.get("model_variant") == "daily_stl_annual":
                lines += ["", f"Annual model: {auto['annual_harmonics']} harmonics, annual peak/trough x{auto['annual_peak_to_trough_ratio']}, "
                              f"outliers masked {auto['outliers_masked']}, holiday multipliers learned for {len(auto['holiday_multipliers'])} holidays."]
        lines.append("")
    if robustness:
        lines += ["## Robustness across synthetic seeds", "",
                  "Daily WAPE % (bias %) for the same tests on synthetic years regenerated with other accepted seeds.", "",
                  "| Seed | Test | current STL | annual STL | weekly naive | yearly naive |", "|---|---|---|---|---|---|"]
        for seed, rows in robustness.items():
            for test, metrics in rows.items():
                cells = [f"{metrics[m]['daily_wape_percent']} ({metrics[m]['bias_percent']:+})" if m in metrics else ""
                         for m in ("off", "auto", "baseline", "baseline_yearly")]
                lines.append(f"| {seed} | {test} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## Caveat", "",
              "The synthetic years take their seasonal shape from the real FY26 year (smoothed and perturbed), so annual-model "
              "scores on FY26 in T1-T3 are an upper bound. T4 uses real data only; T5 is synthetic-only.", ""]
    (out_dir / "backtest_summary.md").write_text("\n".join(lines), encoding="utf-8")
    serializable = {test: {k: v for k, v in (r.items() if "pooled" not in r else {"pooled": r["pooled"], "folds": [
        {kk: vv for kk, vv in f.items() if kk != "daily"} for f in r["folds"]]}.items()) if k != "daily"}
        for test, r in results.items()}
    (out_dir / "backtest_summary.json").write_text(json.dumps(
        {"headline": headline(results), "results": serializable, "robustness": robustness}, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--three-year", type=Path, default=THREE_YEAR)
    parser.add_argument("--real", type=Path, default=REAL)
    parser.add_argument("--out-dir", type=Path, default=SYNTH_DIR / "results")
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--tests", default="T1,T2,T3,T4,T5")
    parser.add_argument("--robustness-seeds", type=int, default=2,
                        help="Extra accepted synthetic seeds to rerun T1 and T2 on (0 to skip).")
    args = parser.parse_args()

    events_path = SYNTH_DIR / "synthetic_events.json"
    events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.exists() else []
    event_days = REAL_EVENT_DAYS + [e["date"] for e in events if e.get("enabled", True)]
    report = SYNTH_DIR / "synthetic_report.json"
    seed_note = f"Synthetic seed: {json.loads(report.read_text())['seed']}." if report.exists() else ""

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        results = run_suite(args.three_year, args.real, temp_dir, args.interval_minutes, event_days,
                            set(args.tests.split(",")))
        robustness = {}
        if args.robustness_seeds:
            import synthesize_queue_years as synth
            for skip in range(1, args.robustness_seeds + 1):
                seed, generated, _ = synth.first_passing(events=events or None, real_path=args.real, skip=skip)
                print(f"Robustness: synthetic seed {seed}", flush=True)
                frames = [year["frame"] for year in generated["years"].values()] + [generated["real"][generated["header"]]]
                path = temp_dir / f"three_year_seed{seed}.csv"
                synth.write_csv(pd.concat(frames, ignore_index=True), generated["header"], path)
                rerun = run_suite(path, args.real, temp_dir, args.interval_minutes, event_days, {"T1", "T2"})
                robustness[seed] = headline(rerun)
        write_outputs(results, robustness, args.out_dir, seed_note)
    print((args.out_dir / "backtest_summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
