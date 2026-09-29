# API Documentation — CDR STL Forecast & Agent Scheduling API

**Version:** `4.1.0`

**Default base URL:** `http://127.0.0.1:8000`

Interactive documentation:

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- OpenAPI schema: `/openapi.json`

## Endpoint summary

| Method | Path | Content type | Description |
|---|---|---|---|
| `GET` | `/` | — | Serve dashboard or return service information |
| `GET` | `/health` | — | Return health and version |
| `POST` | `/api/v1/cdr/stl-forecast` | `multipart/form-data` | Forecast calls using rolling profiles and calculate Erlang C staffing |
| `POST` | `/api/v1/cdr/stl-forecast/async` | `multipart/form-data` | Submit background forecast job |
| `GET` | `/api/v1/jobs/{job_id}` | JSON response | Read job progress and completed result |
| `GET` | `/api/v1/jobs/{job_id}/stream` | `text/event-stream` response | Stream job progress |
| `POST` | `/api/v1/schedule/monthly` | `application/json` | Generate monthly agent roster |

---

## GET `/`

If `static/index.html` exists, the API returns that dashboard file.

Otherwise the fallback response is service information similar to:

```json
{
  "message": "CDR STL Forecast & Agent Scheduling API",
  "documentation": "/docs"
}
```

---

## GET `/health`

### Response

```json
{
  "status": "healthy",
  "version": "4.1.0"
}
```

---

# POST `/api/v1/cdr/stl-forecast`

Forecast future contact volume using recursive rolling profiles and calculate Erlang C staffing for every future interval. The existing stl-forecast URLs are retained for compatibility; the current forecasting method is ROLLING_PROFILE.

## Content type

```text
multipart/form-data
```

## Form fields

| Field | Type | Default | Required | Validation |
|---|---|---:|---|---|
| `files` | repeated file | — | yes | 1-10 non-empty `.csv`/`.txt` files; maximum 25 MiB per file |
| `interval_minutes` | integer | `30` | no | > 0 and exact divisor of 1440 |
| `forecast_days` | integer | `365` | no | 1-3650; the default 365 selects the automatic forecast period below |
| `seasonal_period` | integer or omitted | automatic | no | Compatibility metadata; >= 2 when nonzero; omitted/0 resolves to intervals per week |
| `trend_lookback_days` | integer | `90` | no | Compatibility metadata; >= 7 |
| `target_seconds` | number | `20` | no | >= 0 |
| `target_service_level` | number | `80` | no | after percent conversion, strictly between 0 and 1 |
| `shrinkage` | number | `30` | no | after percent conversion, >= 0 and < 1 |
| `max_agents` | integer | `1000` | no | 1-10,000; neither raw nor scheduled staffing may exceed it |
| `include_forecast_rows` | boolean | `true` | no | controls whether full forecast rows are returned |

Both `80` and `0.80` are accepted as an 80% service-level target. Likewise `30` and `0.30` represent 30% shrinkage. Values greater than 1 are divided by 100; values at or below 1 are interpreted as fractions. Exactly `1` means 100% and is rejected for both settings. Send numeric form values without a `%` suffix.

The legacy seasonal_period and trend_lookback_days fields are validated and echoed in responses, but do not control the current rolling-profile calculation.

### Dashboard request settings

The dashboard exposes only file selection, target answer seconds (default `20`), and target service level (default `80`). It sends the following additional fields to the async endpoint:

| Field | Dashboard value |
|---|---|
| `interval_minutes` | `30` |
| `forecast_days` | `365` |
| `seasonal_period` | `336` |
| `trend_lookback_days` | `90` |
| `shrinkage` | `30` |
| `include_forecast_rows` | `true` |
| `max_agents` | Omitted; API default `1000` applies |

The table's display interval defaults to 1 hour and can be changed to 0.5, 1, 2, 4, or 8 hours. This groups returned rows; it does not change the forecast request or rerun Erlang C.

## Forecast dates and automatic duration

With forecast_days omitted or set to 365, the inclusive span from the earliest to latest valid input timestamp determines the prediction period:

| Historical date span | Returned period |
|---|---|
| 1-6 days | Next day |
| 7-27 days | Next 7 days, starting after the last input day |
| 28 days to less than 12 calendar months | Next full calendar month |
| 12 calendar months or more | Next full calendar year |

Days between the first and last valid records count toward this span even when no calls were recorded on them. The response's historical_days counts distinct dates with valid calls; it can be smaller than the span used to select the horizon.

August 1-31, 2026 data returns September 1-30, 2026: days=30 and 1,440 half-hour intervals. A 28-day span ending mid-August also returns September. Annual forecasts include 366 days when the next calendar year is a leap year.

A non-default forecast_days value requests that many days immediately after the latest historical day, except inputs spanning fewer than seven days always predict one day. Both days and parameters.forecast_days report the effective output duration. output_year is the first forecast date's year.

## CDR file schema

The original format has seven columns without a header:

```text
source,destination,call_datetime,duration,disposition,unique_id,caller_id
```

Accepted read attempts:

```text
UTF-16 + tab
UTF-8-SIG + comma
Latin-1 + comma
```

Example supported call timestamp format:

```text
%Y-%b-%d %I:%M:%S %p
```

Example:

```text
2024-Mar-12 03:25:19 PM
```

Duration supports HH:MM:SS or day/hour/minute/second text in header-based exports.

## CDR cleaning

A row is retained only when:

- timestamp parses;
- duration parses;
- source is non-empty;
- destination is non-empty;
- destination is not `s` case-insensitively;
- disposition is `Answered` by default;
- duration is between 1 second and 4 hours by default.

The reader also accepts the header-based call-export schema with `Call ID`, `Date`, `Caller ID`, `Queue`, and `Talk time`. `Date` is parsed as a normal timestamp, `Talk time` is converted from `HH:MM:SS` or day/hour/minute/second text, and positive talk time marks the call as answered.

## Dataset validation

At least one valid answered record must remain in every uploaded file, and the combined input must contain at least one calendar day. Files may contain partial days, partial months, multiple years, or overlapping dates. Missing dates are not rejected.

## Forecast processing

For every dataset:

```text
read file
→ clean CDR
→ build fixed intervals for available records
→ combine all available history
```

The forecast window follows the automatic-duration rules above. Available call intervals from every file are combined; overlapping records are added rather than deduplicated. Missing months are not padded into a full calendar year.

Each forecast day's call profile is estimated from earlier daily profiles. Depending on available profile history, the calculation uses earlier days, matching weekdays, month-position matches, or prior-year period matches, with fallback profiles. Predicted days are then included when predicting later days. The current implementation does not run STL decomposition.

### Future AHT

Future AHT is calculated from historical weighted AHT by time-of-day slot. Missing slots fall back to global weighted historical AHT.

### Erlang C

For every future interval:

```text
traffic = calls * AHT / interval_seconds
```

`pyworkforce.queuing.ErlangC.required_positions()` calculates raw and shrinkage-adjusted positions.

## Example cURL

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/cdr/stl-forecast" \
  -F "files=@calls_2023.csv" \
  -F "files=@calls_2024.csv" \
  -F "interval_minutes=30" \
  -F "forecast_days=365" \
  -F "trend_lookback_days=90" \
  -F "target_seconds=20" \
  -F "target_service_level=80" \
  -F "shrinkage=30" \
  -F "max_agents=1000" \
  -F "include_forecast_rows=true"
```

## Response structure

Representative shape:

```json
{
  "method": "ROLLING_PROFILE",
  "logic": "Expanding day profiles, then weekly, monthly, and prior-year same-period profiles with new records included.",
  "dataset_count": 2,
  "historical_years": [2023, 2024],
  "historical_days": 731,
  "output_year": 2025,
  "days": 365,
  "interval_minutes": 30,
  "seasonal_period": 336,
  "trend_lookback_days": 90,
  "forecast_interval_count": 17520,
  "total_predicted_calls": 123456,
  "average_aht_seconds": 180.5,
  "maximum_raw_agents": 30,
  "maximum_scheduled_agents": 43,
  "average_service_level_percent": 81.2,
  "average_occupancy_percent": 72.4,
  "average_asa_seconds": 14.1,
  "intervals_below_service_target": 10,
  "peak_interval": {
    "interval_start": "2025-07-01T10:30:00",
    "call_volume": 95,
    "scheduled_agents": 43
  },
  "source_data": {
    "dataset_count": 2,
    "historical_years": [2023, 2024],
    "datasets": []
  },
  "decomposition_summary": {
    "historical_intervals": 35088,
    "prediction_start": "2025-01-01T00:00:00",
    "prediction_strategy": "day-to-day, week-to-week, month-to-month, then year-to-year rolling profiles"
  },
  "parameters": {
    "interval_minutes": 30,
    "forecast_days": 365,
    "seasonal_period": 336,
    "trend_lookback_days": 90,
    "target_seconds": 20,
    "target_service_level_percent": 80,
    "shrinkage_percent": 30,
    "max_agents": 1000
  },
  "charts": {
    "monthly": [],
    "daily": [],
    "weekday": [],
    "time_of_day": []
  },
  "forecast": []
}
```

Example values above are illustrative; actual values come from the uploaded CDR data. Summary AHT uses weights `max(call_volume, 1)`. Summary service level, occupancy, and ASA are arithmetic means over intervals. `parameters.target_service_level_percent` and `parameters.shrinkage_percent` echo the original request values, so fractional inputs remain fractional in those fields.

`day_of_year` is the calendar day-of-year, and `weekday` uses 0 for Monday through 6 for Sunday. Timestamps have no timezone suffix.

## Forecast row

```json
{
  "interval_start": "2025-01-01T00:00:00",
  "date": "2025-01-01",
  "day_of_year": 1,
  "month": 1,
  "day": 1,
  "weekday": 2,
  "weekday_name": "Wednesday",
  "hour": 0,
  "minute": 0,
  "call_volume": 10,
  "aht_seconds": 180.0,
  "traffic_erlangs": 1.0,
  "raw_agents": 3,
  "scheduled_agents": 5,
  "service_level_percent": 80.0,
  "probability_waiting_percent": 20.0,
  "occupancy_percent": 60.0,
  "asa_seconds": 10.0
}
```

If `include_forecast_rows=false`, `forecast` is returned as `[]` while summaries and charts are still generated.

## Error responses

User/data errors return status `400`, for example:

```json
{
  "detail": "Upload at least one CDR dataset."
}
```

Unexpected internal forecast errors return status `500` with:

```json
{
  "detail": "STL forecast failed: ..."
}
```

---

# POST `/api/v1/cdr/stl-forecast/async`

Accepts the same multipart fields as the synchronous forecast endpoint. After saving the uploads, it submits a background job and returns HTTP 200:

```json
{
  "status": "queued",
  "job_id": "example-job-id",
  "message": "Forecast processing started asynchronously.",
  "poll_url": "/api/v1/jobs/example-job-id",
  "stream_url": "/api/v1/jobs/example-job-id/stream"
}
```

The submission response does not contain the forecast. Processing failures are reported in the job's `error` field with `status: failed`. Upload and request errors can occur before a job is created.

# GET `/api/v1/jobs/{job_id}`

Returns HTTP 200 with job status and the forecast result when complete:

```json
{
  "job_id": "example-job-id",
  "status": "processing",
  "progress": 45,
  "message": "Processing forecast...",
  "error": null,
  "result": null
}
```

Statuses are `queued`, `processing`, `completed`, or `failed`. Progress ranges from 0 to 100. When completed, `result` has the same forecast response structure as the synchronous endpoint. Read `result.forecast` for later scheduling requests. Unknown job IDs return HTTP 404.

# GET `/api/v1/jobs/{job_id}/stream`

Streams server-sent events as `text/event-stream` when progress or status changes. Each `data:` JSON object contains `job_id`, `status`, `progress`, `message`, `error`, and `has_result`. The stream closes after `completed` or `failed`. Fetch the polling endpoint to retrieve the completed forecast; the stream does not contain the full result. Unknown job IDs return HTTP 404.

---

# POST `/api/v1/schedule/monthly`

Generate a monthly schedule from forecast staffing requirements.

## Request model

Send the forecast rows returned by the sync endpoint or result.forecast from a completed job. A minimal valid JSON example is:

```json
{
  "forecast": [
    {"interval_start": "2026-09-01T06:00:00", "scheduled_agents": 2},
    {"interval_start": "2026-09-01T06:30:00", "scheduled_agents": 3}
  ],
  "year": 2026,
  "month": 9,
  "agent_count": 8,
  "shift_start_times": ["06:00", "14:00", "22:00"]
}
```

This small example demonstrates the request format. Supply the complete forecast for actual monthly staffing, including adjacent dates when available for overnight shifts.

| Field | Type | Validation |
|---|---|---|
| forecast | array | 1-40,000 rows containing interval_start and scheduled_agents |
| year | integer | Required |
| month | integer | 1-12 |
| agent_count | integer/null | Optional; 1-10,000 and at least the estimated minimum; null calculates it |
| shift_start_times | array of strings/null | Optional; exactly three HH:MM times, eight hours apart in shift order |

Save the JSON above as schedule-request.json, then run:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/schedule/monthly" -H "Content-Type: application/json" --data-binary "@schedule-request.json"
```

In PowerShell, use curl.exe for these curl examples.

## Shift model

Each shift lasts exactly eight hours. Defaults are 00:00, 08:00 and 16:00. With the example above:

| Shift code | Start | End |
|---|---|---|
| NIGHT (1st shift) | 06:00 | 14:00 |
| MORNING (2nd shift) | 14:00 | 22:00 |
| EVENING (3rd shift) | 22:00 | 06:00 the next day |

Codes remain stable identifiers even when custom times change their time of day. For example, 06:15, 14:15 and 22:15 are valid. Times that overlap or leave gaps, such as 06:00, 14:00 and 23:00, return HTTP 400. A list with a length other than three fails request validation with HTTP 422.

An overnight assignment belongs to its start date. Required agents are the peak scheduled_agents over forecast intervals overlapping the full shift, including the next date when supplied. At a forecast boundary only available intervals can be evaluated; the first date's early hours belong to the previous date's overnight assignment.

summary.shifts reports code, name, start_hour, end_hour and label for all three shifts. end_hour can exceed 24 for overnight shifts. Schedule rows use the configured time range in shift.

## Headcount model

For each Monday-Sunday week:

```text
weekly capacity headcount = ceil(total required shift slots / 5)
```

The calculation also considers the maximum total agents required on any one day.

The final minimum employee count is the maximum required value across the month. Null/omitted `agent_count` uses this estimate. If the estimate is zero, automatic generation fails the positive-agent-count check.

The estimate does not guarantee full coverage under the greedy assignment and rest constraints. HTTP 200 can contain `summary.coverage_ok: false`; inspect `coverage_shortage` and the per-shift `coverage` rows. Months are generated independently, without prior-month assignments for boundary rest or weekly-limit checks.

## Assignment rules

- no more than one shift per agent per day;
- no more than five working days per Monday-Sunday week;
- at least eight hours between successive working shifts within the generated month;
- unassigned dates become `OFF`;
- assignments are balanced using existing weekly workload, total assignments, shift-specific assignments, and agent ID.

## Response

Illustrative response with default shift times (values are not the result of the minimal request above):

```json
{
  "summary": {
    "year": 2025,
    "month": 1,
    "minimum_agents": 30,
    "agent_count": 30,
    "shift_hours": 8,
    "shifts": [
      {"code": "NIGHT", "name": "Night", "start_hour": 0, "end_hour": 8, "label": "00:00-08:00"},
      {"code": "MORNING", "name": "Morning", "start_hour": 8, "end_hour": 16, "label": "08:00-16:00"},
      {"code": "EVENING", "name": "Evening", "start_hour": 16, "end_hour": 24, "label": "16:00-00:00"}
    ],
    "working_days_per_week": 5,
    "days_off_per_week": 2,
    "total_required_shift_assignments": 600,
    "total_assigned_shift_assignments": 600,
    "coverage_shortage": 0,
    "coverage_ok": true,
    "coverage": []
  },
  "schedule": []
}
```

Working schedule row:

```json
{
  "agent_id": "Agent 001",
  "date": "2025-01-01",
  "weekday": "Wednesday",
  "shift_code": "MORNING",
  "shift": "08:00-16:00",
  "status": "WORK"
}
```

OFF schedule row:

```json
{
  "agent_id": "Agent 001",
  "date": "2025-01-02",
  "weekday": "Thursday",
  "shift_code": "OFF",
  "shift": "OFF",
  "status": "OFF"
}
```

---

# Rest-rule behaviour

Monthly generation enforces at least eight hours between consecutive work shifts within the generated month, using configured shift times. Monthly schedules are generated independently.

---

# Request limits and examples

Limits are defined in `app/config.py` and the request models in `app/schemas/schedule.py`:

| Input | Limit |
|---|---|
| Forecast uploads | 10 files, 25 MiB per file |
| Requested forecast horizon | 1-3650 days |
| `max_agents` / supplied monthly `agent_count` | 1-10,000 |
| Forecast rows in monthly JSON requests | 1-40,000 |

These array limits apply to incoming scheduling requests; generating a large forecast or roster does not guarantee it fits a subsequent request. Supply the relevant month's source forecast rows and available neighboring intervals for overnight shifts when needed. The CSV's aggregated display columns do not replace the original API fields.

Request examples using `[...]` are schematic: replace each placeholder with the actual returned forecast or schedule rows before sending JSON.

# HTTP error behaviour

## 400 Bad Request

Used for expected validation/data failures.

Shape:

```json
{
  "detail": "Human-readable reason"
}
```

## 404 Not Found

Returned when a polling or streaming request uses an unknown job ID. Jobs are lost when the server process restarts.

## 413 Content Too Large

Returned when an uploaded file exceeds `25 * 1024 * 1024` bytes (25 MiB).

## 422 Unprocessable Entity

FastAPI/Pydantic can return `422` before endpoint logic runs when the JSON/request field type violates the declared request model, for example a monthly schedule `month` outside its Pydantic bounds.

## 500 Internal Server Error

Used for unexpected exceptions. Messages are prefixed by operation, for example:

```text
STL forecast failed: ...
Monthly schedule generation failed: ...
```

---

# Dashboard filtering and CSV exports

CSV downloads are assembled in the browser. The forecast and scheduling endpoints return JSON; there is no dedicated CSV or yearly-roster endpoint.

## Forecast CSV

**Download Forecast CSV** produces `stl_erlang_forecast_<year>_hourly.csv` using all available forecast rows in the output year. It ignores the dashboard's selected date and display interval and groups source rows into 60-minute wall-clock intervals.

| CSV column (in order) | Source / calculation |
|---|---|
| `Interval` | Hour range, such as `2026-01-01 00:00 - 01:00` |
| `Calls` | Sum of `call_volume` |
| `AHT` | Call-weighted `aht_seconds`, formatted to two decimals |
| `Raw agents` | Maximum `raw_agents` within the hour |
| `Scheduled agents` | Maximum `scheduled_agents` within the hour |
| `Service level %` | Call-weighted `service_level_percent`, formatted to two decimals |

For zero-call hours, AHT and service level use arithmetic averages of the source intervals. These calculations match the dashboard table. The last hourly label ends at `24:00`. A full-year export contains 8,760 data rows, or 8,784 in a leap year. Forecasts created before leap-day support must be regenerated to include February 29.

## Daily scheduled agents CSV

**Download Daily Scheduled Agents CSV** exports the exact displayed table: Date and the three shift columns, including custom shift times and all displayed counts. The filename is daily_scheduled_agents_<year>-<month>.csv. No additional months are generated.

Both CSV downloads use UTF-8 with BOM and CRLF line endings, with commas, quotes and newlines escaped.

---

# State handling

The API does not store forecasts or schedules in a database. Async job results live in process memory and disappear on restart. The dashboard keeps its forecast and current schedule in browser memory until a new forecast starts or the page is reloaded. Monthly schedule requests supply the forecast rows.

The leave and shift-swap APIs have been removed.

The CSV downloads are generated by the browser; there is no dedicated CSV API endpoint. Removed leave and swap routes return HTTP 404.
