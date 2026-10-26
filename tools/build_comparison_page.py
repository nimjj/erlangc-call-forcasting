"""Build a self-contained drill-down page: forecast trained on the mirrored years vs the real client year.

Trains the annual STL on the mirrored FY24 + FY25 calls, forecasts the real FY26 year, and writes one HTML
file (data embedded, Chart.js from a CDN) that opens straight from disk, so no API server is needed. The page
has a calls view and an agents-per-shift view (agents the real calls needed vs agents the plan schedules):

    python tools/build_comparison_page.py
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.forecasting.backtest import run_backtest  # noqa: E402
from app.core.forecasting.calendar import us_holidays  # noqa: E402
from app.schemas.schedule import MonthlyScheduleRequest  # noqa: E402
from multi_year_backtest import REAL_EVENT_DAYS, Scorer  # noqa: E402
from shift_coverage_check import POINT, shifts_for  # noqa: E402

THREE_YEAR = ROOT / "testdata" / "Synthetic_years" / "Queue_All_Calls_2023-10_to_2026-09_3yr.csv"
TEMPLATE = Path(__file__).resolve().parent / "comparison_template.html"
OUT = ROOT / "testdata" / "comparison" / "forecast_vs_actual.html"

SHIFT_CODES = ("NIGHT", "MORNING", "EVENING")
POLICY_FIELDS = ("coverage_percent", "night_minimum_agents", "weekend_minimum_agents", "target_seconds", "target_service_level", "shrinkage")

warnings.filterwarnings("ignore", message="Could not infer format")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=THREE_YEAR, help="mirrored years + real year in one CDR file")
    parser.add_argument("--train-end", default="2025-10-01", help="first real day; earlier rows train the forecast")
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    result = run_backtest(
        [args.data], train_end=args.train_end, interval_minutes=args.interval_minutes, annual_seasonality="auto",
    )
    split = result["split"]
    holidays = us_holidays(split["test_start"], split["test_end"])
    tags = {day.date().isoformat(): name for day, name in holidays.items()}
    for day in REAL_EVENT_DAYS:
        tags[day] = f"{tags[day]} + event" if day in tags else "Event"

    # Agents per shift: what the real calls needed, the plan the API builds by default, and the old point-forecast rule.
    policy = {name: MonthlyScheduleRequest.model_fields[name].default for name in POLICY_FIELDS}
    scorer = Scorer(args.data, args.interval_minutes, REAL_EVENT_DAYS)
    needed = shifts_for(result, scorer, args.train_end, "actual", POINT)
    planned = shifts_for(result, scorer, args.train_end, "predicted", {**POINT, **policy})
    old_rule = shifts_for(result, scorer, args.train_end, "predicted", POINT)
    shifts = {
        day.date().isoformat(): [[int(needed[(day, code)]), int(planned[(day, code)]), int(old_rule[(day, code)])] for code in SHIFT_CODES]
        for day in needed.index.get_level_values("date").unique()
    }

    slots: dict[str, list[list[int]]] = {}
    for row in result["intervals"]:
        slots.setdefault(row["interval_start"][:10], []).append([row["actual"], row["predicted"]])
    payload = {
        "split": split,
        "method": result["forecast_method"],
        "variant": result["decomposition_summary"].get("model_variant"),
        "interval_minutes": result["interval_minutes"],
        "metrics": {"model": result["metrics"]["model"], "yearly_naive": result["metrics"].get("seasonal_naive_yearly")},
        "days": [
            {"d": d["date"], "a": d["actual"], "p": d["predicted"], "y": d.get("baseline_yearly"), "tag": tags.get(d["date"])}
            for d in result["daily"]
        ],
        "slots": slots,
        "shifts": shifts,
        "policy": policy,
    }

    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(payload, separators=(",", ":")))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")

    model = result["metrics"]["model"]
    print(f"{result['forecast_method']} / {payload['variant']}  train {split['train_start']} to {split['train_end']} "
          f"({split['train_calls']:,} calls), test {split['test_start']} to {split['test_end']} ({split['test_calls']:,} calls)")
    print(f"actual {model['total_actual_calls']:,}  predicted {model['total_predicted_calls']:,}  "
          f"bias {model['bias_percent']}%  daily WAPE {model['daily_wape_percent']}%  interval WAPE {model['interval_wape_percent']}%")
    busy = needed > 0
    print(f"agents per shift: needed {int(needed[busy].sum())}, planned {int(planned.sum())}; "
          f"shifts covered {100 * (planned[busy] >= needed[busy]).mean():.1f}% (old rule {100 * (old_rule[busy] >= needed[busy]).mean():.1f}%)")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
