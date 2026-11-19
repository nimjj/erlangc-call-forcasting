"""Checks for the PR #1 service review fixes. Run: python -m unittest discover -s tests -t ."""
from __future__ import annotations

import threading
import time
import unittest

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.main import RequestGuard, app
from app.workers import compute
from app.workers.task_manager import TaskManager
from tests.test_review_fixes import synthetic_calls


def cdr_csv(calls) -> str:
    return "Caller ID,Queue,Date,Talk Time,Unique ID\n" + "\n".join(
        f"100,200,{when},{duration},{index}" for index, (when, duration) in enumerate(calls)
    )


THREE_WEEKS = cdr_csv(synthetic_calls(pd.date_range("2026-01-01", periods=21, freq="D"), 60, 3))


class WorkIsBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def forecast(self, files, **data):
        return self.client.post("/api/v1/cdr/stl-forecast", files=files, data=data)

    def test_a_stray_old_row_is_refused_quickly(self):
        stray = cdr_csv([("2000-06-01 10:00:00", "0:03:00")])
        started = time.monotonic()
        response = self.forecast(
            [("files", ("old.csv", stray, "text/csv")), ("files", ("new.csv", THREE_WEEKS, "text/csv"))],
            interval_minutes=15, annual_seasonality="off", forecast_days=7,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("stray rows", response.json()["detail"])
        self.assertLess(time.monotonic() - started, 30)

    def test_a_horizon_too_big_for_its_interval_is_refused(self):
        response = self.forecast([("files", ("a.csv", THREE_WEEKS, "text/csv"))], forecast_days=3650, interval_minutes=5)
        self.assertEqual(response.status_code, 400)
        self.assertIn("rows", response.json()["detail"])

    def test_a_normal_forecast_still_works(self):
        response = self.forecast([("files", ("a.csv", THREE_WEEKS, "text/csv"))], forecast_days=7)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["forecast"]), 7 * 48)

    def test_health_answers_while_a_forecast_runs(self):
        done = threading.Event()

        def run():
            self.forecast([("files", ("a.csv", THREE_WEEKS, "text/csv"))], forecast_days=28, interval_minutes=15)
            done.set()

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.5)
        slowest = 0.0
        while not done.is_set():
            started = time.monotonic()
            self.assertEqual(self.client.get("/health").status_code, 200)
            slowest = max(slowest, time.monotonic() - started)
            time.sleep(0.2)
        worker.join()
        self.assertLess(slowest, 1.0)


class PercentagesAreChecked(unittest.TestCase):
    def test_a_fractional_shrinkage_is_refused_before_a_job_exists(self):
        client = TestClient(app)
        response = client.post(
            "/api/v1/cdr/stl-forecast/async",
            files=[("files", ("a.csv", THREE_WEEKS, "text/csv"))], data={"shrinkage": 0.5},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("percentage", response.json()["detail"])

    def test_service_level_must_be_a_percentage(self):
        client = TestClient(app)
        response = client.post(
            "/api/v1/cdr/stl-forecast",
            files=[("files", ("a.csv", THREE_WEEKS, "text/csv"))], data={"target_service_level": 0.8},
        )
        self.assertEqual(response.status_code, 422)


def tiny_app(**guard) -> TestClient:
    inner = FastAPI()

    @inner.post("/api/echo")
    async def echo(request: Request):
        return {"bytes": len(await request.body())}

    return TestClient(RequestGuard(inner, **guard))


class RequestGuardTests(unittest.TestCase):
    def test_a_declared_oversized_body_is_refused(self):
        response = tiny_app(max_bytes=100).post("/api/echo", content=b"x" * 101)
        self.assertEqual(response.status_code, 413)

    def test_a_streamed_oversized_body_is_refused(self):
        def chunks():
            for _ in range(10):
                yield b"x" * 50

        response = tiny_app(max_bytes=100).post("/api/echo", content=chunks())
        self.assertEqual(response.status_code, 413)

    def test_a_body_within_the_limit_passes(self):
        self.assertEqual(tiny_app(max_bytes=100).post("/api/echo", content=b"x" * 100).json(), {"bytes": 100})

    def test_internal_key(self):
        client = tiny_app(max_bytes=1000, api_key="secret")
        self.assertEqual(client.post("/api/echo", content=b"x").status_code, 401)
        self.assertEqual(client.post("/api/echo", content=b"x", headers={"X-Internal-Key": "nope"}).status_code, 401)
        self.assertEqual(client.post("/api/echo", content=b"x", headers={"X-Internal-Key": "secret"}).status_code, 200)


class Jobs(unittest.TestCase):
    def test_finished_jobs_are_dropped_after_the_ttl(self):
        manager = TaskManager(max_workers=1, ttl_seconds=0)
        job_id = manager.create_job()
        manager.update_job(job_id, status="completed", result={"forecast": [1, 2, 3]})
        time.sleep(0.01)
        self.assertIsNone(manager.get_job(job_id))

    def test_a_job_stays_queued_until_a_worker_starts_it(self):
        manager = TaskManager(max_workers=1)
        gate = threading.Event()
        first = manager.create_job()
        manager.submit_task(first, lambda job_id: gate.wait(5))
        second = manager.create_job()
        manager.submit_task(second, lambda job_id: None)
        self.assertEqual(manager.get_job(second).status, "queued")
        gate.set()

    def test_unexpected_errors_are_not_echoed(self):
        manager = TaskManager(max_workers=1)
        job_id = manager.create_job()

        def boom(job_id):
            raise KeyError("/secret/internal/path")

        manager.submit_task(job_id, boom)
        for _ in range(50):
            if manager.get_job(job_id).status == "failed":
                break
            time.sleep(0.02)
        job = manager.get_job(job_id)
        self.assertEqual(job.status, "failed")
        self.assertNotIn("secret", f"{job.error} {job.message}")

    def test_a_full_service_answers_503(self):
        taken = []
        while compute.reserve_slot():
            taken.append(True)
        try:
            response = TestClient(app).post(
                "/api/v1/cdr/stl-forecast/async", files=[("files", ("a.csv", THREE_WEEKS, "text/csv"))],
            )
            self.assertEqual(response.status_code, 503)
        finally:
            for _ in taken:
                compute.release_slot()


if __name__ == "__main__":
    unittest.main()
