import unittest
import pandas as pd
from fastapi import HTTPException
from app.api.v1.schedule import monthly_schedule
from app.schemas.schedule import MonthlyScheduleRequest
from app.core.constants import SHIFT_DEFINITIONS
from app.core.scheduling.shifts import build_shift_requirements, shift_definitions
from app.core.scheduling.rest import validate_agent_rest_period


class ManualShiftTimesTests(unittest.TestCase):
    def forecast(self, start="2026-09-01", end="2026-10-01 06:00"):
        return pd.DataFrame({"interval_start": pd.date_range(start, end, freq="30min"),
                             "scheduled_agents": 1})

    def test_default_compatibility(self):
        self.assertEqual(shift_definitions(), SHIFT_DEFINITIONS)
        a = build_shift_requirements(self.forecast(), 2026, 9)
        b = build_shift_requirements(self.forecast(), 2026, 9, ["00:00", "08:00", "16:00"])
        pd.testing.assert_frame_equal(a, b, check_dtype=False)

    def test_validate_times_and_overnight_labels(self):
        shifts = shift_definitions(["06:15", "14:15", "22:15"])
        self.assertEqual(shifts[-1]["label"], "22:15-06:15")
        self.assertEqual(shifts[-1]["end_hour"], 30.25)
        for bad in ([], ["06:00"], ["24:00", "08:00", "16:00"],
                    ["06:00", "14:00", "23:00"], ["06:00", "06:00", "14:00"],
                    ["6:00", "14:00", "22:00"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                shift_definitions(bad)

    def test_overnight_uses_next_date_and_next_month(self):
        frame = self.forecast()
        frame.loc[frame.interval_start == pd.Timestamp("2026-09-02 03:00"), "scheduled_agents"] = 9
        frame.loc[frame.interval_start == pd.Timestamp("2026-10-01 03:00"), "scheduled_agents"] = 11
        req = build_shift_requirements(frame, 2026, 9, ["06:00", "14:00", "22:00"])
        for date, peak in [("2026-09-01", 9), ("2026-09-30", 11)]:
            row = req.loc[(req.date == date) & (req.shift_code == "EVENING")].iloc[0]
            self.assertEqual(row.required_agents, peak)

    def test_minute_boundary_includes_overlapping_interval(self):
        frame = self.forecast()
        frame.loc[frame.interval_start == pd.Timestamp("2026-09-01 14:00"), "scheduled_agents"] = 7
        req = build_shift_requirements(frame, 2026, 9, ["06:15", "14:15", "22:15"])
        day = req.loc[req.date == "2026-09-01"]
        self.assertEqual(day.loc[day.shift_code == "NIGHT", "required_agents"].iloc[0], 7)
        self.assertEqual(day.loc[day.shift_code == "MORNING", "required_agents"].iloc[0], 7)

    def test_api_schedule_labels_and_rest(self):
        request = MonthlyScheduleRequest(forecast=self.forecast().to_dict("records"),
            year=2026, month=9, agent_count=12, shift_start_times=["22:00", "06:00", "14:00"])
        result = monthly_schedule(request)
        self.assertTrue(result["summary"]["coverage_ok"])
        self.assertEqual([s["label"] for s in result["summary"]["shifts"]],
                         ["22:00-06:00", "06:00-14:00", "14:00-22:00"])
        frame = pd.DataFrame(result["schedule"])
        for agent, rows in frame.loc[frame.status == "WORK"].groupby("agent_id"):
            self.assertFalse(rows.date.duplicated().any())
            for row in rows.itertuples():
                self.assertTrue(validate_agent_rest_period(frame, agent, row.date)["valid"])

    def test_custom_rest_rejects_overnight_to_morning(self):
        frame = pd.DataFrame([
            {"agent_id": "A", "date": "2026-09-01", "shift_code": "NIGHT", "shift": "22:00-06:00", "status": "WORK"},
            {"agent_id": "A", "date": "2026-09-02", "shift_code": "MORNING", "shift": "06:00-14:00", "status": "WORK"},
        ])
        self.assertFalse(validate_agent_rest_period(frame, "A", "2026-09-02")["valid"])
        self.assertFalse(validate_agent_rest_period(frame, "A", "2026-09-01")["valid"])

    def test_invalid_api_times_return_400(self):
        request = MonthlyScheduleRequest(forecast=self.forecast().to_dict("records"),
            year=2026, month=9, shift_start_times=["06:00", "14:00", "23:00"])
        with self.assertRaises(HTTPException) as error:
            monthly_schedule(request)
        self.assertEqual(error.exception.status_code, 400)
