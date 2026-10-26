"""Check a running API end to end: health, a forecast, a background job, and a monthly schedule.

Uses generated call data (three weeks of made-up calls), so it needs no files and sends nothing real.
Standard library only, so it runs anywhere Python does:

    python tools/smoke_test.py [--base-url http://127.0.0.1:8000]

Exits 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

FIRST_DAY = datetime(2025, 3, 3)  # a Monday
HISTORY_DAYS = 21
RESULTS: list[bool] = []


def make_cdr_csv() -> bytes:
    """Three weeks of calls in the header-style export: busy weekdays, quiet weekends."""
    rng = random.Random(1)
    lines = ["Call ID,Date,Caller ID,Queue,Agent,Wait time,Ringing time,Talk time"]
    call_id = 1
    for day in range(HISTORY_DAYS):
        date = FIRST_DAY + timedelta(days=day)
        weekend = date.weekday() >= 5
        for slot in range(16, 38):  # 08:00 to 19:00
            for _ in range(rng.choice((0, 0, 1) if weekend else (1, 2, 2, 3))):
                when = date + timedelta(minutes=slot * 30 + rng.randrange(30), seconds=rng.randrange(60))
                talk = rng.randrange(120, 900)
                lines.append(
                    f"{call_id},{when:%Y-%m-%d %H:%M:%S},555{rng.randrange(10**6, 10**7)},Support,Agent/{rng.randrange(1, 9)},"
                    f"00:00:05,00:00:03,{talk // 3600:02d}:{talk % 3600 // 60:02d}:{talk % 60:02d}"
                )
                call_id += 1
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def request(method: str, url: str, body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": content_type} if content_type else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def upload(url: str, fields: dict[str, str], filename: str, content: bytes) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode() for name, value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode())
    parts.append(content + f"\r\n--{boundary}--\r\n".encode())
    return request("POST", url, b"".join(parts), f"multipart/form-data; boundary={boundary}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="where the API is listening")
    base = parser.parse_args().base_url.rstrip("/")

    try:
        status, health = request("GET", f"{base}/health")
    except (urllib.error.URLError, OSError) as error:
        print(f"FAIL  cannot reach {base}: {error}")
        return 1
    if not check("health", status == 200 and health.get("status") == "healthy", f"version {health.get('version')}"):
        return 1

    csv_bytes = make_cdr_csv()
    fields = {"interval_minutes": "30", "target_seconds": "20", "target_service_level": "80", "shrinkage": "30"}

    status, forecast = upload(f"{base}/api/v1/cdr/stl-forecast", fields, "smoke_test.csv", csv_bytes)
    rows = forecast.get("forecast") or []
    check("forecast", status == 200 and forecast.get("method") == "STL" and len(rows) == 7 * 48,
          f"HTTP {status}, {forecast.get('method')}, {len(rows)} intervals, {forecast.get('total_predicted_calls')} calls")
    needed = {"interval_start", "call_volume", "aht_seconds", "scheduled_agents"}
    check("forecast rows carry what the scheduler needs", bool(rows) and needed <= set(rows[0]))

    status, submitted = upload(f"{base}/api/v1/cdr/stl-forecast/async", fields, "smoke_test.csv", csv_bytes)
    job_state = {}
    if status == 200:
        deadline = time.time() + 120
        while time.time() < deadline:
            _, job_state = request("GET", f"{base}{submitted['poll_url']}")
            if job_state.get("status") in ("completed", "failed"):
                break
            time.sleep(1)
    check("background job completes", job_state.get("status") == "completed", f"status {job_state.get('status')}")

    if rows:
        first = rows[0]["interval_start"]
        payload = {"forecast": rows, "year": int(first[:4]), "month": int(first[5:7])}

        def schedule(**policy) -> tuple[int, dict]:
            return request("POST", f"{base}/api/v1/schedule/monthly", json.dumps({**payload, **policy}).encode(), "application/json")

        status, planned = schedule()
        summary = planned.get("summary", {})
        check("schedule with the default staffing policy", status == 200 and summary.get("staffing_policy", {}).get("coverage_percent") == 80,
              f"HTTP {status}, {summary.get('total_required_shift_assignments')} agent-shifts required")
        status, plain = schedule(coverage_percent=None, night_minimum_agents=0, weekend_minimum_agents=0)
        old = plain.get("summary", {}).get("total_required_shift_assignments")
        check("default policy needs at least as many agent-shifts as the plain forecast", status == 200 and old is not None
              and summary.get("total_required_shift_assignments", 0) >= old, f"{summary.get('total_required_shift_assignments')} vs {old}")
        status, _ = schedule(coverage_percent=40)
        check("invalid coverage_percent is rejected", status == 422, f"HTTP {status}")

    status, _ = request("GET", f"{base}/api/v1/jobs/does-not-exist")
    check("unknown job returns 404", status == 404, f"HTTP {status}")

    print(f"\n{sum(RESULTS)} of {len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
