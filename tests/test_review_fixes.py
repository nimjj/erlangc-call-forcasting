"""Checks for the PR #1 review fixes. Run: python -m unittest discover -s tests -t ."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.forecasting.stl import build_stl_forecast
from app.core.forecasting.aggregates import weighted_kpis
from app.core.forecasting.stl import _round_per_day
from app.core.ingestion.cleaner import clean_percent, preprocess_cdr, talk_time_to_seconds
from app.core.queuing.erlang_c import required_agents
from app.core.scheduling.shifts import shift_definitions


def write_cdr(path: Path, calls: list[tuple[pd.Timestamp, str]]) -> Path:
    rows = ["Caller ID,Queue,Date,Talk Time,Unique ID"]
    rows += [f"100,200,{when},{duration},{index}" for index, (when, duration) in enumerate(calls)]
    path.write_text("\n".join(rows) + "\n")
    return path


def synthetic_calls(days: pd.DatetimeIndex, day_calls: int, night_calls: float, seed: int = 1) -> list:
    """`day_calls` spread over 08:00-20:00 and about `night_calls` over 00:00-08:00, per day."""
    rng = np.random.default_rng(seed)
    calls = []
    for day in days:
        for _ in range(day_calls):
            calls.append((day + pd.Timedelta(seconds=int(rng.integers(8 * 3600, 20 * 3600))), "0:03:00"))
        for _ in range(rng.poisson(night_calls)):
            calls.append((day + pd.Timedelta(seconds=int(rng.integers(0, 8 * 3600))), "0:03:00"))
    return calls


class RoundingKeepsQuietIntervals(unittest.TestCase):
    def test_round_per_day_keeps_the_day_total(self):
        day = np.array([0.2] * 16 + [5.3] * 32)
        rounded = _round_per_day(day, 48)
        self.assertEqual(int(rounded.sum()), round(day.sum()))
        self.assertGreater(int(rounded[:16].sum()), 0)

    def test_quiet_nights_still_get_calls_in_the_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = pd.date_range("2026-01-01", periods=60, freq="D")
            path = write_cdr(Path(tmp) / "lo.csv", synthetic_calls(days, 56, 3.4))
            forecast, _ = build_stl_forecast([path], forecast_days=28, annual_seasonality="off")
        night = forecast.loc[forecast["interval_start"].dt.hour < 8]
        agents = night.groupby(night["interval_start"].dt.date)["raw_agents"].max()
        # Rounding each interval to whole calls left most nights at 0 calls, so 0 agents.
        self.assertTrue((agents >= 1).all(), agents.tolist())
        self.assertGreater(forecast["expected_calls"].sum(), 0)


class MissingDaysAreNotZeroCallDays(unittest.TestCase):
    def test_an_export_gap_does_not_drag_the_forecast_down(self):
        days = pd.date_range("2026-01-01", periods=60, freq="D")
        gap = (days >= "2026-02-20") & (days < "2026-02-27")
        with tempfile.TemporaryDirectory() as tmp:
            full = write_cdr(Path(tmp) / "full.csv", synthetic_calls(days, 100, 2))
            holed = write_cdr(Path(tmp) / "gap.csv", synthetic_calls(days[~gap], 100, 2))
            with_all, _ = build_stl_forecast([full], forecast_days=28, annual_seasonality="off")
            with_gap, summary = build_stl_forecast([holed], forecast_days=28, annual_seasonality="off")
        self.assertEqual(summary["decomposition_summary"]["missing_days_filled"], 7)
        ratio = with_gap["call_volume"].sum() / with_all["call_volume"].sum()
        self.assertGreater(ratio, 0.9)


class IngestionKeepsEveryValidRow(unittest.TestCase):
    def test_durations_in_any_common_format(self):
        self.assertEqual(talk_time_to_seconds("0:03:05"), 185)
        self.assertEqual(talk_time_to_seconds("03:05"), 185)
        self.assertEqual(talk_time_to_seconds("185"), 185)
        self.assertEqual(talk_time_to_seconds("1m 30s"), 90)
        self.assertIsNone(talk_time_to_seconds("soon"))

    def test_mixed_durations_and_date_formats_in_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cdr(Path(tmp) / "mixed.csv", [
                ("2026-02-14 10:00:00", "0:03:05"),
                ("02/15/2026 10:00", "02:00"),
                ("2026-02-16T10:00:00", "1m 30s"),
                ("2026-02-17 10:00:00", "95"),
            ])
            clean = preprocess_cdr(path)
        self.assertEqual(len(clean), 4)
        self.assertEqual(clean["duration_seconds"].tolist(), [185, 120, 90, 95])


class Percentages(unittest.TestCase):
    def test_percent_sign_and_whole_numbers_are_percentages(self):
        self.assertAlmostEqual(clean_percent("80%"), 0.8)
        self.assertAlmostEqual(clean_percent(80), 0.8)
        self.assertAlmostEqual(clean_percent(0.8), 0.8)
        self.assertAlmostEqual(clean_percent("1%"), 0.01)
        self.assertAlmostEqual(clean_percent(1), 0.01)
        self.assertAlmostEqual(clean_percent("0.5%"), 0.005)


class ErlangC(unittest.TestCase):
    def test_textbook_case(self):
        # 200 calls/hour, AHT 180s, 20s target, 80% -> 14 agents.
        result = required_agents(100, 180, 1800, 20, 80)
        self.assertEqual(result["raw_agents"], 14)
        self.assertAlmostEqual(result["service_level"], 0.88835, places=4)

    def test_does_not_ask_for_one_agent_too_many(self):
        # A = 0.5 erlangs: one agent already reaches 81.6%.
        result = required_agents(15, 60, 1800, 120, 80)
        self.assertEqual(result["raw_agents"], 1)
        self.assertGreaterEqual(result["service_level"], 0.8)

    def test_zero_target_seconds_is_rejected_up_front(self):
        with self.assertRaisesRegex(ValueError, "Target seconds"):
            required_agents(100, 180, 1800, 0, 80)

    def test_occupancy_cap(self):
        uncapped = required_agents(1000, 300, 1800, 20, 80)
        capped = required_agents(1000, 300, 1800, 20, 80, max_occupancy=0.85)
        self.assertGreater(uncapped["occupancy"], 0.9)
        self.assertLessEqual(capped["occupancy"], 0.85)
        self.assertGreater(capped["raw_agents"], uncapped["raw_agents"])


class Shifts(unittest.TestCase):
    def test_the_shift_over_the_middle_of_the_night_is_night(self):
        shifts = shift_definitions(["06:00", "14:00", "22:00"])
        night = next(shift for shift in shifts if shift["code"] == "NIGHT")
        self.assertEqual(night["label"], "22:00-06:00")
        self.assertEqual(shift_definitions(["00:00", "08:00", "16:00"])[0]["label"], "00:00-08:00")


class HeadlineKpis(unittest.TestCase):
    def test_empty_intervals_do_not_flatter_service_level(self):
        frame = pd.DataFrame({
            "call_volume": [0, 0, 0, 100],
            "service_level_percent": [100, 100, 100, 70],
            "asa_seconds": [0, 0, 0, 40],
            "traffic_erlangs": [0, 0, 0, 10],
            "raw_agents": [0, 0, 0, 12],
        })
        kpis = weighted_kpis(frame)
        self.assertAlmostEqual(kpis["average_service_level_percent"], 70)
        self.assertAlmostEqual(kpis["average_asa_seconds"], 40)
        self.assertAlmostEqual(kpis["average_occupancy_percent"], 10 / 12 * 100)


if __name__ == "__main__":
    unittest.main()
