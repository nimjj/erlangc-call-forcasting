# API Documentation — CDR STL Forecast & Agent Scheduling API

**Version:** `4.4.0`

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
| `POST` | `/api/v1/cdr/stl-forecast` | `multipart/form-data` | Forecast calls using STL (rolling fallback for short history) and calculate Erlang C staffing |
| `POST` | `/api/v1/cdr/stl-forecast/async` | `multipart/form-data` | Submit background forecast job |
| `POST` | `/api/v1/cdr/backtest` | `multipart/form-data` | Train before a split date, forecast the held-out days, and score accuracy |
| `GET` | `/api/v1/jobs/{job_id}` | JSON response | Read job progress and completed result |
| `GET` | `/api/v1/jobs/{job_id}/stream` | `text/event-stream` response | Stream job progress |
| `POST` | `/api/v1/schedule/monthly` | `application/json` | Generate monthly agent roster |

---

## GET `/`

If `static/index.html` exists, the API returns that dashboard file. This is the case when you run the app locally with Uvicorn.

The Docker image is backend-only and does not contain `static/`, so in the container `/` always returns the fallback below and `/static/...` returns 404.

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
  "version": "4.4.0"
}
```

---

# POST `/api/v1/cdr/stl-forecast`

Forecast future contact volume using robust STL decomposition and calculate Erlang C staffing for every future interval. Histories of at least 365 days use a daily STL model with annual seasonality and US holiday effects (unless `annual_seasonality=off`). Spans under 14 days, or histories shorter than two requested seasonal cycles, use a labeled rolling-profile fallback. The response reports `method: STL` or `method: ROLLING_PROFILE`, and `decomposition_summary.model_variant` is `daily_stl_annual` or `interval_stl_weekly` for STL.

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
| `seasonal_period` | integer or omitted | automatic | no | STL cycle length in intervals; >= 2 when nonzero; omitted/0 resolves to intervals per week |
| `trend_lookback_days` | integer | `90` | no | Recent days of STL trend used for linear extrapolation; >= 7 |
| `annual_seasonality` | `auto` or `off` | `auto` | no | `auto` uses the daily model with annual seasonality and US holidays when history spans at least 365 days; `off` keeps the interval-level weekly STL |
| `target_seconds` | number | `20` | no | >= 0 |
| `target_service_level` | number | `80` | no | after percent conversion, strictly between 0 and 1 |
| `shrinkage` | number | `30` | no | after percent conversion, >= 0 and < 1 |
| `max_agents` | integer | `1000` | no | 1-10,000; neither raw nor scheduled staffing may exceed it |
| `include_forecast_rows` | boolean | `true` | no | controls whether full forecast rows are returned |

Both `80` and `0.80` are accepted as an 80% service-level target. Likewise `30` and `0.30` represent 30% shrinkage. Values greater than 1 are divided by 100; values at or below 1 are interpreted as fractions. Exactly `1` means 100% and is rejected for both settings. Send numeric form values without a `%` suffix.

`seasonal_period` controls the STL decomposition cycle. `trend_lookback_days` selects how much of the extracted trend is used for its fitted forecast line, capped by available history. These parameters are validated and returned for all requests but are not used to calculate rolling fallback predictions.

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

Read attempts use the pandas Python parser and check recognized headers before trying seven headerless columns. Upload names without an extension default to `.csv`.

The reader also accepts the header-based call-export schema with `Call ID`, `Date`, `Caller ID`, `Queue`, and `Talk time`. `Date` is parsed as a normal timestamp, `Talk time` is converted from `HH:MM:SS` or day/hour/minute/second text, and rows are marked answered before duration cleaning; only durations from 1 second through 4 hours survive. Other export columns are ignored.

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

The forecast window follows the automatic-duration rules above independently of method selection. Calls from overlapping uploads are added, not deduplicated.

For STL, the history is reindexed to a continuous interval grid from the first valid date at midnight through the last valid date at day end. Missing intervals and whole dates are zero-filled, including partial-day boundaries. This assumes complete CDR coverage; missing uploads are interpreted as zero demand.

When the historical span is at least 14 days and the grid contains at least `2 * seasonal_period` intervals, `statsmodels.tsa.seasonal.STL(..., robust=True)` fits trend, seasonal, and residual components. A straight line fitted to the recent extracted trend is extrapolated, and the last fitted seasonal cycle is repeated. Calendar gaps between history and the forecast window advance both components. Their sum is clipped at zero and rounded to whole calls; residual noise is not included in predictions, and forecast values are not refitted as history.

Otherwise the method is `ROLLING_PROFILE`: earlier daily patterns are averaged recursively, using matching weekdays/calendar positions where available. The response sets `decomposition_summary.applied=false` and includes `fallback_reason`. Errors fitting an eligible STL series are reported as errors, not silently converted to rolling forecasts.

The interval STL models one seasonal period and has no yearly component. These are point forecasts without prediction intervals; linear extrapolation can overstate or understate distant demand. Evaluate accuracy against held-out observations.

### Annual seasonality (365+ days of history)

With `annual_seasonality=auto` (default) and a historical span of at least 365 days, `app/core/forecasting/annual.py` forecasts daily totals on a `log1p` scale instead:

1. Robust weekly STL (period 7) on daily totals, refitted after replacing US holidays and outlier days with fitted values.
2. An annual Fourier curve over day of year (2 harmonics for 1-2 years of history, 3 for 2+ years), fitted jointly with a linear spline trend with one knot per year.
3. A linear trend over the last `trend_lookback_days` of the deseasonalised series, damped by 0.6 per week; the level is calibrated to reproduce the last 28 training days' call total.
4. Holiday multipliers learned from past occurrences of each observed US federal holiday, Thanksgiving eve, the day after Thanksgiving, Christmas Eve and New Year's Eve, blended with class defaults.
5. Each day split into intervals with the weekday's intraday profile from the last 26 weeks; largest-remainder rounding keeps interval counts summing to the daily total.

`decomposition_summary` adds `model_variant: "daily_stl_annual"`, `annual_seasonality: true`, `annual_harmonics`, `annual_peak_to_trough_ratio`, `trend_knots_days`, `trend_lookback_days_used`, `trend_slope_per_day_log`, `trend_damping_per_week`, `terminal_level_calls_per_day`, `level_calibration_factor`, `level_calibration_days`, `weekly_factors`, `holiday_days_adjusted`, `holiday_multipliers`, `outliers_masked`, `outlier_dates`, `residual_std_log`, and `predicted_daily_mean`. When the annual model does not run, `annual_seasonality` is `false` and `annual_seasonality_reason` explains why.

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
  "method": "STL",
  "logic": "Robust STL decomposition with recent linear trend extrapolation and repeating seasonal cycle.",
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
    "prediction_strategy": "Robust STL decomposition with recent linear trend extrapolation and repeating seasonal cycle.",
    "applied": true,
    "observed_intervals": 20000,
    "zero_filled_intervals": 15088,
    "historical_span_days": 731,
    "seasonal_period": 336,
    "robust": true,
    "trend_lookback_intervals": 4320,
    "trend_start": 3.2,
    "trend_end": 4.1,
    "trend_slope_per_interval": 0.00001,
    "seasonal_min": -2.0,
    "seasonal_max": 3.5,
    "residual_std": 0.9,
    "reconstruction_max_error": 0.0
  },
  "parameters": {
    "interval_minutes": 30,
    "forecast_days": 365,
    "seasonal_period": 336,
    "trend_lookback_days": 90,
    "annual_seasonality": "auto",
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

For rolling fallback, `decomposition_summary` retains the grid counts, span, period, prediction start, and strategy, but sets `applied=false` and adds `fallback_reason`. STL-only fields such as `trend_slope_per_interval` and `residual_std` are omitted. `historical_intervals` includes zero-filled slots; `observed_intervals` counts slots containing cleaned calls. `reconstruction_max_error` measures the historical reconstruction error of trend + seasonal + residual, not forecast accuracy.

## Chart response fields and dashboard calculations

| Array | Fields and meaning |
|---|---|
| `charts.monthly` | `month_label`, total `call_volume`, `max_scheduled_agents`, mean `average_occupancy_percent`, mean `average_service_level_percent` |
| `charts.daily` | `date`, total `call_volume`, `max_scheduled_agents`, mean `average_service_level_percent`, mean `average_occupancy_percent`, mean `average_asa_seconds` |
| `charts.weekday` | `weekday_name` in Monday-Sunday order, `average_calls` per source interval across the entire forecast, `max_scheduled_agents` |
| `charts.time_of_day` | `time_label` in HH:MM order, `average_calls` and `average_scheduled_agents` per time slot across the entire forecast |

Aggregates are rounded to two decimal places. The monthly grouping uses month number/name without year, so it combines matching months across years.

The dashboard's **Average daily calls by weekday** chart uses daily totals, not `charts.weekday.average_calls`. A selected week shows the call total for each date. All-weeks mode divides each weekday's total calls by the number of forecast dates for that weekday in the selected month, including zero-call dates. Weeks follow Monday-Sunday calendar boundaries; days outside the month or missing forecast dates are blank. Zero calls and missing data can both appear empty, but only real zero-call dates count in averages.

Clicking a month selects its first calendar week; clicking a weekday bar in a specific week selects its exact date. The time chart averages source intervals by time slot for the selected week/month or shows one selected date. If no interval rows are available, it falls back to overall `charts.time_of_day` averages. Chart selections do not rerun the forecast.

The dashboard calendar uses the first forecast year, and the table/forecast CSV filter to the output year. Multi-year API forecasts should be analyzed directly from the returned rows; the dashboard does not provide separate chart navigation for each year.

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

# POST `/api/v1/cdr/backtest`

Measures forecast accuracy on held-out data. Records before `train_end` are used for training, and the forecast for the remaining days is compared with the calls that actually arrived and with simple baselines. The standalone page in `backtest-ui/` calls this endpoint; see the README section "Checking quality".

The request is synchronous. It returns the full result in one response and runs on a worker thread, so other requests stay responsive while it computes.

## Content type

```text
multipart/form-data
```

## Form fields

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `files` | repeated file | — | yes | 1-10 non-empty `.csv`/`.txt` files, 25 MiB maximum each, covering both the training and test periods. Same format and cleaning as `/api/v1/cdr/stl-forecast`. |
| `train_end` | date (`YYYY-MM-DD`) | first record date + 14 days | no | First held-out day. Records before it are training data; records from it onward are test data. |
| `interval_minutes` | integer | `30` | no | Must divide 1440 |
| `seasonal_period` | integer | one week of intervals | no | Same meaning as in the forecast endpoint |
| `trend_lookback_days` | integer | `90` | no | At least 7; limited to the available training history |
| `annual_seasonality` | `auto` or `off` | `auto` | no | `auto` uses the daily model with annual seasonality and US holidays when history spans at least 365 days; `off` keeps the interval-level weekly STL |
| `target_seconds` | number | `20` | no | Passed to the staffing calculation |
| `target_service_level` | number | `80` | no | Passed to the staffing calculation |
| `shrinkage` | number | `30` | no | Passed to the staffing calculation |
| `max_agents` | integer | `1000` | no | 1-10,000 |

## Split rules

- Both sides of the split must contain valid answered calls. Otherwise the response is HTTP 400: `Split date ... must fall inside the data range ... with records on both sides.`
- The training period must span at least 7 days, or the response is HTTP 400.
- STL needs at least 14 training days and two seasonal cycles. With less, the forecast uses the rolling-profile fallback: `forecast_method` is `ROLLING_PROFILE` and `fallback_reason` explains why.
- The forecast starts the day after the last training day that has calls and runs to the last day in the data. Only intervals on or after `train_end` are scored.
- The forecast horizon is always the exact held-out span. The automatic horizon rule of the forecast endpoint does not apply.

## Baseline and metrics

The **seasonal-naive baseline** repeats the last 7 days of training data, interval for interval. It uses no test data. A useful forecast should beat it.

When the training span is at least 371 days, a **yearly seasonal-naive baseline** is added: each interval takes the value from the same weekday and time 364 days earlier (or a multiple of 364 days, so the source is always training data), multiplied by the ratio of calls in the last 91 training days to the same 91 days a year earlier (clipped to 0.5-2.0). It is reported as `metrics.seasonal_naive_yearly` (with `yoy_ratio`) and as `baseline_yearly` in the `daily` and `intervals` rows.

`metrics.model` scores the forecast and `metrics.seasonal_naive` (and `seasonal_naive_yearly`, when present) score the baselines, using the same fields:

| Field | Meaning |
|---|---|
| `interval_mae` | Mean absolute error in calls per interval |
| `interval_rmse` | Root-mean-square error in calls per interval |
| `interval_wape_percent` | Σ\|predicted − actual\| ÷ Σ actual across intervals. Very high at low volumes. |
| `total_actual_calls`, `total_predicted_calls` | Calls over the test period |
| `bias_percent` | (predicted − actual) ÷ actual. Positive means over-forecasting. |
| `daily_mape_percent` | Mean absolute % error of daily totals, skipping days with zero actual calls |
| `daily_wape_percent` | Σ\|daily error\| ÷ Σ daily actual. The best single accuracy figure. |

Percentage fields are `null` when the actual total is zero.

## Example cURL

```bash
curl -X POST http://127.0.0.1:8000/api/v1/cdr/backtest \
  -F "files=@calls_2026-08.csv" \
  -F "train_end=2026-08-15" \
  -F "interval_minutes=30"
```

## Response structure

The `daily` and `intervals` arrays below are shortened:

```json
{
  "split": {
    "data_start": "2026-08-01",
    "data_end": "2026-08-31",
    "train_start": "2026-08-01",
    "train_end": "2026-08-14",
    "train_span_days": 14,
    "train_calls": 443,
    "test_start": "2026-08-15",
    "test_end": "2026-08-31",
    "test_days": 17,
    "test_calls": 447
  },
  "forecast_method": "STL",
  "forecast_logic": "Robust STL decomposition with recent linear trend extrapolation and repeating seasonal cycle.",
  "fallback_reason": null,
  "decomposition_summary": {
    "historical_intervals": 672,
    "historical_span_days": 14,
    "prediction_start": "2026-08-15T00:00:00",
    "seasonal_period": 336,
    "applied": true,
    "...": "same fields as the forecast endpoint's decomposition_summary"
  },
  "interval_minutes": 30,
  "annual_seasonality": "auto",
  "metrics": {
    "model": {
      "interval_mae": 0.551,
      "interval_rmse": 1.214,
      "interval_wape_percent": 100.67,
      "total_actual_calls": 447,
      "total_predicted_calls": 553,
      "bias_percent": 23.71,
      "daily_mape_percent": 29.36,
      "daily_wape_percent": 31.77
    },
    "seasonal_naive": {
      "interval_mae": 0.561,
      "interval_rmse": 1.25,
      "interval_wape_percent": 102.46,
      "total_actual_calls": 447,
      "total_predicted_calls": 561,
      "bias_percent": 25.5,
      "daily_mape_percent": 29.52,
      "daily_wape_percent": 31.77
    }
  },
  "daily": [
    { "date": "2026-08-15", "weekday": "Saturday", "actual": 9, "predicted": 8, "baseline": 8 }
  ],
  "intervals": [
    { "interval_start": "2026-08-15T08:30:00", "actual": 0, "predicted": 1, "baseline": 1 }
  ]
}
```

| Field | Meaning |
|---|---|
| `split` | Data range, training and test ranges, day spans, and call counts |
| `forecast_method` | `STL` or `ROLLING_PROFILE` |
| `fallback_reason` | Why STL was not applied, or `null` |
| `decomposition_summary` | Same structure as in the forecast response |
| `annual_seasonality` | The requested `annual_seasonality` value; `decomposition_summary.model_variant` shows which model ran |
| `metrics` | Accuracy for the forecast (`model`), the weekly baseline (`seasonal_naive`), and, with 371+ training days, the yearly baseline (`seasonal_naive_yearly`) |
| `daily` | Daily totals of actual, predicted, and baseline calls (plus `baseline_yearly` when present) for each test day |
| `intervals` | Actual, predicted, and baseline calls (plus `baseline_yearly` when present) for each scored interval |

Errors follow the same rules as the forecast endpoint: HTTP 400 for data and split problems, 413 for oversized files, and 500 prefixed with `Backtest failed:`.

---

# Cross-origin (CORS) access

Browser pages served from another origin, such as the backtest page on `http://localhost:5500`, can call the API only when their origin is listed in the `CORS_ALLOW_ORIGINS` environment variable (comma-separated). When the variable is unset, the API sends no CORS headers. [compose.yaml](compose.yaml) defaults it to `http://localhost:5500,http://127.0.0.1:5500`. Allowed methods are `GET` and `POST`. Non-browser clients such as cURL are not affected.

---

# POST `/api/v1/schedule/monthly`

Generate a monthly schedule from forecast staffing requirements.

## Request model

Send the forecast rows returned by the sync endpoint or result.forecast from a completed job. A minimal valid JSON example is:

```json
{
  "forecast": [
    {"interval_start": "2026-09-01T06:00:00", "call_volume": 3, "aht_seconds": 640.0, "scheduled_agents": 5},
    {"interval_start": "2026-09-01T06:30:00", "call_volume": 2, "aht_seconds": 640.0, "scheduled_agents": 3}
  ],
  "year": 2026,
  "month": 9,
  "agent_count": 8,
  "shift_start_times": ["06:00", "14:00", "22:00"]
}
```

The API roster covers every calendar date in the requested month, while coverage requirements use dates present in the forecast. The dashboard daily staffing table displays only forecast dates in that month.

This small example demonstrates the request format. Supply the complete forecast for actual monthly staffing, including adjacent dates when available for overnight shifts.

| Field | Type | Validation |
|---|---|---|
| forecast | array | 1-40,000 rows containing interval_start, scheduled_agents, and (unless coverage_percent is null) call_volume and aht_seconds |
| year | integer | Required |
| month | integer | 1-12 |
| agent_count | integer/null | Optional; 1-10,000 and at least the estimated minimum; null calculates it |
| shift_start_times | array of strings/null | Optional; exactly three HH:MM times, eight hours apart in shift order |
| coverage_percent | number/null | Default 80; 50-99. Share of shifts to cover fully; null uses the largest scheduled_agents per shift |
| night_minimum_agents | integer | Default 3; 0-10,000. Minimum agents on the night shift |
| weekend_minimum_agents | integer | Default 3; 0-10,000. Minimum agents on Saturday and Sunday day shifts |
| target_seconds, target_service_level, shrinkage | numbers | Defaults 20, 80, 30. Use the values the forecast was made with |

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

An overnight assignment belongs to its start date. Required agents come from the forecast intervals overlapping the full shift (see Shift staffing), including the next date when supplied. At a forecast boundary only available intervals can be evaluated; the first date's early hours belong to the previous date's overnight assignment.

summary.shifts reports code, name, start_hour, end_hour and label for all three shifts. end_hour can exceed 24 for overnight shifts. Schedule rows use the configured time range in shift.

## Shift staffing

By default each shift is sized to the busiest interval it is likely to see, not to the average forecast. A shift needs the Erlang C agents for its busiest interval, and the busiest interval of a day is usually busier than the forecast average, so staffing from the point forecast covers only about half of all shifts (55% in the mirrored-data test; 51% over rolling monthly folds).

With `coverage_percent` set (default 80), every shift is simulated 300 times: call counts are Poisson around the forecast `call_volume`, with a small day-level variation for forecast error, converted to agents with Erlang C using the forecast `aht_seconds`, `target_seconds`, `target_service_level` and `shrinkage`. The shift requires the agents that cover `coverage_percent` of those simulated shifts. Results are repeatable: the simulation uses a fixed seed.

`night_minimum_agents` and `weekend_minimum_agents` (both default 3) then raise the night shift, and the day shifts on Saturday and Sunday, to at least that many agents, because a single call needs 3 agents at the default targets and a quiet shift cannot be forecast by call count alone. Set a minimum to 0 to turn it off.

Send `coverage_percent: null`, `night_minimum_agents: 0` and `weekend_minimum_agents: 0` to get the previous rule, which needs only `scheduled_agents`: each shift requires the largest `scheduled_agents` among its intervals.

Measured on the mirrored-data test (trained on the mirrored years, planned for the real year), the defaults covered about 93% of shifts with demand (about 94% over rolling monthly folds) against 55% before, at the price of roughly 45% more agent-shifts than the bare minimum, about two thirds of which are the minimums on shifts that had no calls. `tools/shift_coverage_check.py` reproduces these figures; raise or lower `coverage_percent` to trade cover for cost.

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
- unassigned dates become `OFF`, including dates without forecast demand;
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

For zero-call hours, AHT and service level use arithmetic averages of the source intervals. These calculations match the dashboard table. The last hourly label ends at `24:00`. A full-year export contains 8,760 data rows, or 8,784 in a leap year. Shorter forecasts export only the available dates; the filename does not imply a complete year.

## Daily scheduled agents CSV

**Download Daily Scheduled Agents CSV** exports the exact displayed table: Date and the three shift columns, including custom shift times and all displayed counts. The filename is daily_scheduled_agents_<year>-<month>.csv. No additional months are generated.

Both CSV downloads use UTF-8 with BOM and CRLF line endings, with commas, quotes and newlines escaped.

---

# State handling

The API does not store forecasts or schedules in a database. Async job results live in process memory and disappear on restart. The dashboard keeps its forecast and current schedule in browser memory until a new forecast starts or the page is reloaded. Monthly schedule requests supply the forecast rows.

The leave and shift-swap APIs have been removed.

The CSV downloads are generated by the browser; there is no dedicated CSV API endpoint. Removed leave and swap routes return HTTP 404.
