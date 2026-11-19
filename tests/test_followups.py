"""Checks for the follow-ups to the PR #1 review. Run: python -m unittest discover -s tests -t ."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.forecasting.annual import _holiday_multipliers
from app.core.forecasting.stl import build_stl_forecast
from app.core.scheduling.shifts import build_shift_requirements
from app.schemas.schedule import MonthlyScheduleRequest
from tests.test_review_fixes import synthetic_calls, write_cdr


def night_calls_per_day(calls: list) -> float:
    nights = sum(1 for when, _ in calls if pd.Timestamp(when).hour < 8)
    days = len({pd.Timestamp(when).normalize() for when, _ in calls})
    return nights / days


class QuietNightsWithAYearOfHistory(unittest.TestCase):
    """With 365+ days the daily annual model is used; it used to hand staffing whole calls only."""

    def test_annual_model_keeps_fractional_expected_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = pd.date_range("2026-01-01", periods=400, freq="D")
            path = write_cdr(Path(tmp) / "year.csv", synthetic_calls(days, 56, 3.4))
            forecast, summary = build_stl_forecast([path], forecast_days=28)
        self.assertTrue(summary["decomposition_summary"]["annual_seasonality"])
        self.assertTrue((forecast["call_volume"] == forecast["call_volume"].round()).all())
        self.assertFalse((forecast["expected_calls"] == forecast["expected_calls"].round()).all())
        night = forecast.loc[forecast["interval_start"].dt.hour < 8]
        agents = night.groupby(night["interval_start"].dt.date)["raw_agents"].max()
        self.assertTrue((agents >= 1).all(), agents.tolist())


class SparseNightsAreNotForecastAsNoise(unittest.TestCase):
    def setUp(self):
        self.days = pd.date_range("2026-01-01", periods=60, freq="D")
        self.calls = synthetic_calls(self.days, 56, 3.4)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cdr(Path(tmp) / "lo.csv", self.calls)
            self.forecast, self.summary = build_stl_forecast([path], forecast_days=28, annual_seasonality="off")

    def test_night_level_follows_the_history(self):
        night = self.forecast.loc[self.forecast["interval_start"].dt.hour < 8]
        per_day = night["expected_calls"].sum() / 28
        self.assertAlmostEqual(per_day / night_calls_per_day(self.calls), 1.0, delta=0.25)
        self.assertGreater(self.summary["decomposition_summary"]["residual_bias_cycles"], 0)

    def test_busy_hours_keep_their_total(self):
        per_day = self.forecast["expected_calls"].sum() / 28
        history = len(self.calls) / len(self.days)
        self.assertAlmostEqual(per_day / history, 1.0, delta=0.05)


class HolidaysComeFromTheHistory(unittest.TestCase):
    def test_a_normal_seen_holiday_is_not_cut_and_an_unseen_one_is_not_adjusted(self):
        result = _holiday_multipliers({"Christmas Day": [1.0]}, {"Christmas Day", "Labor Day"})
        self.assertEqual(result, {"Christmas Day": 1.0, "Labor Day": 1.0})

    def test_a_dip_the_history_shows_is_kept(self):
        self.assertAlmostEqual(_holiday_multipliers({"Christmas Day": [0.2, 0.3]}, {"Christmas Day"})["Christmas Day"], 0.25)

    def test_multipliers_stay_within_bounds(self):
        self.assertEqual(_holiday_multipliers({"Thanksgiving Eve": [9.0]}, {"Thanksgiving Eve"})["Thanksgiving Eve"], 3.0)


def busy_week() -> pd.DataFrame:
    """A week of 30-minute intervals with 300 calls an interval from 08:00 to 16:00, otherwise none."""
    index = pd.date_range("2026-03-02", periods=7 * 48, freq="30min")
    busy = (index.hour >= 8) & (index.hour < 16)
    return pd.DataFrame({
        "interval_start": index,
        "call_volume": np.where(busy, 300, 0),
        "expected_calls": np.where(busy, 300.0, 0.0),
        "aht_seconds": 180.0,
        "scheduled_agents": np.where(busy, 90, 0),
    })


class ScheduleHonoursMaxOccupancy(unittest.TestCase):
    def requirements(self, max_occupancy: float) -> pd.DataFrame:
        return build_shift_requirements(
            busy_week(), year=2026, month=3, coverage_percent=80,
            night_minimum_agents=0, weekend_minimum_agents=0, max_occupancy=max_occupancy,
        )

    def test_a_cap_needs_more_agents_at_the_peak(self):
        uncapped = self.requirements(100)["required_agents"]
        capped = self.requirements(85)["required_agents"]
        self.assertTrue((capped >= uncapped).all())
        self.assertGreater(capped.max(), uncapped.max())

    def test_the_request_defaults_to_85(self):
        request = MonthlyScheduleRequest(
            forecast=[{"interval_start": "2026-03-02T08:00:00", "scheduled_agents": 3}], year=2026, month=3,
        )
        self.assertEqual(request.max_occupancy, 85)


if __name__ == "__main__":
    unittest.main()
