# CDR STL Forecast & Agent Scheduling

FastAPI application for forecasting contact-centre call demand from historical CDR files using STL decomposition with a rolling-profile fallback for short histories, calculating Erlang C staffing, and generating monthly agent rosters.

Forecasts use robust seasonal-trend decomposition with LOESS (STL) when the historical date span is at least 14 days and contains two seasonal cycles. With at least 365 days of history, the forecast switches to a daily STL model that also learns yearly seasonality and US holiday effects (`annual_seasonality=auto`, the default). Shorter histories use rolling profiles. Responses identify the actual method as `STL` or `ROLLING_PROFILE`, and `decomposition_summary.model_variant` says which STL model ran; the dashboard completion message shows the method and any fallback reason.

**Application version:** `4.3.0`

The application is **dockerized as a backend-only REST API**. The container serves the API alone; it does not include a browser frontend. Two browser pages ship in the repository for use outside the container:

| Page | Location | Purpose |
|---|---|---|
| Dashboard | [static/index.html](static/index.html) | Full forecasting and scheduling UI. Served at `/` only when you run the app locally with Uvicorn (not in Docker). |
| Backtest page | [backtest-ui/index.html](backtest-ui/index.html) | Standalone page that checks forecast accuracy against held-out data. Talks to the API (local or containerized) over REST. See [Testing with the backtest page](#testing-with-the-backtest-page). |
| Drill-down page | [backtest-ui/drilldown.html](backtest-ui/drilldown.html) | Same backtest, plotted as predicted vs actual lines that you can zoom from month to week to day to half-hour to see where the forecast misses. Served the same way as the backtest page, at `http://localhost:5500/drilldown.html`. |

[API documentation](api_documentation.md) contains endpoint examples and response details.

## How it works

```text
                     CDR files (.csv / .txt, 1-10 files, <= 25 MiB each)
                                         |
          +------------------------------+------------------------------+
          |                              |                              |
 POST /api/v1/cdr/stl-forecast   POST .../stl-forecast/async    POST /api/v1/cdr/backtest
   (synchronous result)          (job_id -> poll /jobs/{id})    (train_end split date)
          |                              |                              |
          +------------------------------+--------------+---------------+
                                         |              |
                                         v              | backtest only: keep records
           1. Read     read_cdr_csv           reader.py | before train_end for training,
              detect encoding/separator, map headers    | hold out the rest as "actuals"
                                         |              |
                                         v              |
           2. Clean    preprocess_cdr         cleaner.py
              answered calls only, parse dates and talk time,
              keep durations from 1 s to 4 h
                                         |
                                         v
           3. Bucket   build_cdr_intervals    interval.py
              call volume and handle time per 15/30/60-minute interval
                                         |
                                         v
           4. Choose method                              stl.py / annual.py
              history >= 14 days AND >= 2 seasonal cycles (1 week = 336 x 30-min)?
                     |                                            |
                    yes: history >= 365 days and                  no
                    annual_seasonality=auto?                      |
                     |                     |                      |
                    yes                    no                     v
                     v                     v              Rolling-profile fallback
           Daily STL + annual       Interval STL          average of matching past days
           Fourier + US holidays,   trend + weekly        (same weekday / month position)
           damped trend, weekday    seasonal + residual
           intraday profiles        (linear trend,
                     |               repeating cycle)
                     |                     |                      |
                     +---------------------+----------------------+
                                      v
           5. Average handling time per time-of-day slot (from history)
                                      |
                                      v
           6. Erlang C staffing   queuing/erlang_c.py, staffing.py
              agents needed per interval for the target service level, plus shrinkage
                                      |
            +-------------------------+--------------------------+
            v                                                    v
   Forecast response (JSON)                         Backtest scoring    backtest.py
   summary, charts, per-interval rows               predicted vs actual held-out calls
            |                                       and vs seasonal-naive baselines
            v                                       (last training week; same weekday
   POST /api/v1/schedule/monthly                    a year earlier): MAE, RMSE, WAPE, MAPE, bias
   roster.py, shifts.py: 3 x 8-hour shifts,
   daily agent counts, rest rules
```

## Run with Docker

Docker Compose builds and runs the API as one container. Docker Desktop (or Docker Engine with the Compose plugin) is required.

```powershell
docker compose up --build -d
```

Check it with `http://127.0.0.1:8000/health`. Interactive API docs are at `http://127.0.0.1:8000/docs`. Opening `/` returns a short JSON service message, because the container has no dashboard.

To use another local port, set `APP_PORT` before starting Compose:

```powershell
$env:APP_PORT = "8080"
docker compose up --build -d
```

View logs with `docker compose logs -f app`, and stop the container with `docker compose down`.

### What's inside the container

| Item | Details |
|---|---|
| Base image | `python:3.12-slim` |
| Application code | `app/` only. `static/`, `backtest-ui/`, `testdata/`, docs, and Postman files are excluded by [.dockerignore](.dockerignore). |
| Dependencies | Exact versions pinned in [requirements.txt](requirements.txt), so every build produces the same forecasts. |
| Process | `uvicorn app.main:app` with one worker on port 8000, published only on `127.0.0.1` by default |
| User | Non-root `appuser` (UID 10001) |
| Filesystem | Read-only root filesystem. Uploads go to a 512 MiB `/tmp` tmpfs and are deleted after processing. Logs persist in the `app-logs` volume at `/app/logs`. |
| Hardening | All Linux capabilities dropped, `no-new-privileges` |
| Health check | Docker polls `/health` every 30 seconds |

| Environment variable | Default (in [compose.yaml](compose.yaml)) | Purpose |
|---|---|---|
| `APP_PORT` | `8000` | Host port mapped to the container |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5500,http://127.0.0.1:5500` | Comma-separated browser origins allowed to call the API. The default allows the backtest page. Set it to your frontend's origin in production, or to an empty value to disable cross-origin browser access. |

This deployment intentionally runs one Uvicorn worker because background jobs and results are held in process memory. Do not scale this service to multiple containers or workers until job state is moved to shared durable storage. For remote access, place it behind an authenticated HTTPS reverse proxy; the application itself does not provide authentication.

## Testing with the backtest page

The backtest page checks forecast accuracy. It trains the model on the records before a split date, forecasts the remaining days, and compares the forecast with what actually happened.

1. Start the API: `docker compose up --build -d`
2. Serve the page from the repository root: `python -m http.server 5500 --directory backtest-ui`
3. Open `http://localhost:5500`. The status next to the API URL turns green when the API is reachable.
4. Choose a CDR file, set the **split date** (the first held-out day), and select **Run backtest**.

For the month-to-week-to-day-to-interval view of the same results, open `http://localhost:5500/drilldown.html`. Tick "Also draw the old method" to overlay the weekly-only forecast; it runs the backtest twice and the old method takes about a minute on a year of data.

Serve the page as shown rather than opening the HTML file directly. A file opened from disk has a `null` origin, which CORS rejects.

Reading the results:

- **Method badge:** `STL` means true STL ran. `ROLLING_PROFILE` means the training period was shorter than 14 days, so the fallback ran instead.
- **Forecast vs baselines:** the weekly naive baseline repeats the last training week. With at least 371 training days a yearly naive baseline (same weekday 52 weeks earlier, scaled by the year-over-year ratio) is added. If the forecast doesn't beat them, the history is too short for STL to add value.
- **Annual seasonality:** `Auto` uses the annual model once training covers 365 days; `Off` forces the interval-level weekly STL so you can compare the two.
- **Daily WAPE / MAPE:** the most useful accuracy figures. Interval-level errors look very large at low call volumes, because being off by one call in a half hour is a 100% miss.
- **Total bias:** positive means over-forecasting, which leads to overstaffing.

The training period must cover at least 7 days, and at least 14 days for STL to apply. The page calls `POST /api/v1/cdr/backtest`; see the [API documentation](api_documentation.md#post-apiv1cdrbacktest).

## Quick start

Use Python 3.10 or later. Dependencies are listed in [requirements.txt](requirements.txt): pandas, NumPy, statsmodels, pyworkforce, FastAPI, Uvicorn, and python-multipart. There is no frontend build step.

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Run commands from the repository root. The compatibility entry point `python -m uvicorn api:app --reload` also works.

| URL | Purpose |
|---|---|
| `/` | Dashboard when run locally (JSON service message in Docker or when `static/` is absent) |
| `/health` | Health and application version |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | Generated OpenAPI schema |

The dashboard loads Chart.js from jsDelivr. The application creates `logs/` when needed and requires write access for logs and temporary uploads. `/static` is mounted only when the `static/` directory exists.

## Project structure

```text
Calculator-Erlang-C/
|-- api.py                         # Compatibility entry point
|-- app/                           # Everything the Docker image contains
|   |-- main.py                    # App setup, routers, CORS, optional static files, request logging
|   |-- config.py                  # Paths, application limits, CORS_ALLOW_ORIGINS
|   |-- logger.py                  # Console and daily rotating file logs
|   |-- api/
|   |   |-- health.py              # Dashboard and health routes
|   |   `-- v1/                    # Forecast, backtest, schedule, and job routes
|   |-- schemas/                   # Pydantic models; routes define actual responses
|   |-- core/
|   |   |-- constants.py           # CDR fields, forecast fields, shift definitions
|   |   |-- ingestion/             # CDR parsing, cleaning, interval construction
|   |   |-- forecasting/           # STL/annual/fallback forecasts, US holiday calendar, backtest scoring, dashboard aggregates
|   |   |-- queuing/               # Erlang C calculations and staffing cache
|   |   `-- scheduling/            # Monthly rosters, shift times, and rest constraints
|   `-- workers/                   # In-memory jobs and background forecast worker
|-- static/index.html              # Local dashboard, filters, and CSV generation (not in the image)
|-- backtest-ui/index.html         # Standalone backtest page served outside the container
|-- Dockerfile                     # Backend-only image
|-- compose.yaml                   # Container runtime settings, port, CORS origins, log volume
|-- .dockerignore
|-- requirements.txt               # Pinned dependency versions
|-- api_documentation.md
```

## Dashboard workflow

1. Upload one or more historical CDR files containing at least one day of valid records.
2. Set **Target answer seconds** and **Target service level %**, then select **Create STL forecast**.
3. Wait for the background job. Review summaries and monthly, weekday, and time-of-day charts.
4. Select a date and table interval to inspect staffing. Download the hourly forecast for the whole output year.
5. Select a schedule month and optionally enter **Available agent count**. Leave it blank for automatic minimum-headcount estimation, then select **Generate Monthly Schedule**.
6. Review the staffing summary and daily scheduled-agent counts for each shift.
7. Select **Download Daily Scheduled Agents CSV** to export the displayed daily staffing table with its current shift times and counts.

## Forecast inputs

### Inputs exposed by the dashboard

The forecast form exposes these three inputs. Other forecasting parameters are supplied by the browser or use the API default.

| Dashboard input | Request field | Initial value | Behaviour |
|---|---|---|---|
| Upload Historical CDR Data | `files` | Required | Multiple `.csv` or `.txt` files; at least one valid calendar day is required |
| Target answer seconds | `target_seconds` | `20` | Number of seconds; browser minimum `0`, step `0.01` |
| Target service level % | `target_service_level` | `80` | Browser range `0.01`-`99.99`, step `0.01`; API percentage conversion described below |

### Parameters used by the dashboard

| Request field | Dashboard value | Meaning |
|---|---|---|
| `interval_minutes` | `30` | Forecast and Erlang C source intervals are 30 minutes |
| `forecast_days` | `365` | Selects the automatic forecast period from the historical date span |
| `seasonal_period` | `336` | Weekly STL cycle: 48 intervals/day multiplied by 7 |
| `trend_lookback_days` | `90` | Recent STL trend window used for linear extrapolation |
| `shrinkage` | `30` | 30% shrinkage |
| `include_forecast_rows` | `true` | Return rows needed by the tables and scheduler |
| `max_agents` | Not sent; API default `1000` | Maximum permitted raw or scheduled staffing per interval |

Changing the forecast table's display interval does not change forecast inputs or rerun Erlang C. The table defaults to **1 hr** and supports **0.5, 1, 2, 4, and 8 hr** groupings. The forecast CSV always uses 1-hour groups.

### Inputs accepted by the forecast API

Both `POST /api/v1/cdr/stl-forecast` and `POST /api/v1/cdr/stl-forecast/async` accept `multipart/form-data` with these fields:

| Field | Type | Default | Validation / meaning |
|---|---|---|---|
| `files` | Repeated uploaded file | Required | 1-10 non-empty `.csv`/`.txt` files; at most 25 MiB per file |
| `interval_minutes` | Integer | `30` | Positive exact divisor of 1440 |
| `forecast_days` | Integer | `365` | 1-3650; `365` selects the automatic forecast period below |
| `seasonal_period` | Integer or omitted | Automatic | STL cycle in intervals; effective period must be at least 2 |
| `trend_lookback_days` | Integer | `90` | Days of STL trend used for extrapolation; at least 7, limited to available history |
| `annual_seasonality` | `auto` or `off` | `auto` | `auto` uses the annual model when history spans at least 365 days; `off` keeps the interval-level weekly STL |
| `target_seconds` | Number | `20` | Non-negative Erlang C answer-time target in seconds |
| `target_service_level` | Number | `80` | Converted fraction must be strictly between 0 and 1 |
| `shrinkage` | Number | `30` | Converted fraction must be at least 0 and less than 1 |
| `max_agents` | Integer | `1000` | 1-10,000; fails if either raw or scheduled positions exceed this value |
| `include_forecast_rows` | Boolean | `true` | `false` returns summaries/charts with an empty `forecast` array |

Percentage conversion divides values greater than 1 by 100; values at or below 1 are interpreted as fractions. Thus `80` and `0.8` both mean 80%, and `30` and `0.3` both mean 30% shrinkage. Exactly `1` means 100% and is rejected for both service level and shrinkage. Submit numeric form values, without a `%` suffix.

Omitting `seasonal_period` uses `(1440 // interval_minutes) * 7`. The current implementation also treats `0` as automatic. A nonzero supplied value must resolve to at least 2 intervals. STL requires at least two cycles and a 14-day historical span. If either requirement is unmet, the response explicitly reports rolling-profile fallback. These parameters control STL, not the fallback calculation.

Valid interval examples include `5`, `10`, `15`, `20`, `30`, `60`, `120`, `240`, and `480`; `35` is invalid. Larger API horizons are supported, but the dashboard and yearly downloads are designed around one output year. Scheduling request arrays have their own row limits.

## CDR input and cleaning

The application accepts the original seven-column format without a header, or the header-based call-export format shown below.

### Header-based call export

These columns are mapped as follows:

| Export column | Forecast meaning |
|---|---|
| `Call ID` | Unique call identifier |
| `Date` | Call date and time; 24-hour timestamps are accepted |
| `Caller ID` | Calling source |
| `Queue` | Destination or queue |
| `Talk time` | Handle time; formats such as `00d 00h 00m 34s` are accepted |
| `Agent`, `Wait time`, `Ringing time`, `Entry`, `Exit`, `Transferred`, `Dumped`, `Ended` | Ignored by the forecast reader; `Talk time` determines valid answered calls |

For this format, rows with a positive `Talk time` are treated as answered calls.

### Canonical headerless format

```text
source,destination,call_datetime,duration,disposition,unique_id,caller_id
```

The reader tries UTF-16 with tabs, UTF-8 with BOM and commas, then Latin-1 with commas. Each attempt uses pandas' Python parser, first checking for recognized column headers and then trying the headerless format.

| Field | Format / use |
|---|---|
| `source` | Non-empty calling identifier |
| `destination` | Non-empty destination; the value `s` is excluded, case-insensitively |
| `call_datetime` | `%Y-%b-%d %I:%M:%S %p`, for example `2025-Jan-15 09:42:11 AM` |
| `duration` | `HH:MM:SS`, for example `00:03:12` |
| `disposition` | Only `Answered` retained, case-insensitively |
| `unique_id` | Read as an input field |
| `caller_id` | Read as an input field |

Cleaning removes unparseable dates/durations, missing source/destination values, excluded destinations/dispositions, and durations outside 1 second to 4 hours inclusive. These cleaning defaults are internal function settings, not forecast form fields.

Every file must retain at least one valid answered call. Records may cover partial days, partial months, multiple years, or overlapping files. At least one calendar day of valid records is required overall; missing dates are not rejected.

## Forecast calculations

### Historical series and method selection

The application combines cleaned records into fixed intervals. Overlapping uploads are added together; duplicate calls are not deduplicated. STL uses a continuous interval grid from midnight on the earliest valid date through the end of the latest valid date. Missing intervals and dates are filled with zero calls, including unrecorded hours at either boundary. This assumes the uploads cover those dates: a missing export is treated as no demand, not unknown demand.

- **STL:** at least 14 calendar days and at least two cycles of `seasonal_period`. The default 336 half-hour intervals represents one week, so 14 days is the minimum.
- **STL with annual seasonality:** at least 365 calendar days and `annual_seasonality=auto` (the default). `decomposition_summary.model_variant` is `daily_stl_annual`; otherwise it is `interval_stl_weekly` and `annual_seasonality_reason` says why the annual model did not run.
- **Rolling-profile fallback:** a span under 14 days, or insufficient history for two cycles of a larger custom period. `decomposition_summary.applied` is false and `fallback_reason` explains why. There is no silent fallback if STL itself fails.

### STL decomposition and forecasting

The implementation uses [`statsmodels.tsa.seasonal.STL`](https://www.statsmodels.org/stable/generated/statsmodels.tsa.seasonal.STL.html) with `robust=True` to separate observed call counts into **trend + seasonal + residual** components.

1. Fit STL once to the historical interval series, using `seasonal_period` as the cycle length.
2. Fit a straight line to the last `trend_lookback_days` of the extracted trend (or all available history if shorter).
3. Extend that fitted trend line and repeat the final fitted seasonal cycle at the correct future calendar positions. Calendar gaps before the next month/year advance both the trend and seasonal phase.
4. Add trend and seasonality, clip negative predictions to zero, and round to whole calls. Residual noise is not added to point forecasts. Predictions are not fed back into STL as observed history.
5. Estimate AHT by historical time-of-day handle time divided by calls, with a global call-weighted fallback, then calculate Erlang C staffing.

This is a single-seasonality model. It does not separately learn yearly seasonality or holidays. Linear trend extrapolation can grow or decline substantially over long horizons; forecasts are point estimates without prediction intervals or guaranteed accuracy. Validate them against held-out call data before operational staffing decisions. Histories of a year or more use the annual model below instead, unless `annual_seasonality=off`.

### Annual seasonality and holidays (365+ days of history)

[`app/core/forecasting/annual.py`](app/core/forecasting/annual.py) forecasts **daily totals** on a `log1p` scale, so weekly, yearly and holiday effects multiply:

1. **Weekly pattern:** robust STL with a 7-day period on daily totals. US holidays and outlier days (more than 4 robust standard deviations, on days expecting at least 5 calls) are replaced with fitted values, and STL is refitted.
2. **Yearly pattern:** a Fourier curve over the day of the year (2 harmonics with 1-2 years of history, 4 with 2+ years), fitted together with a linear spline trend that has one knot per year. Fitting both together stops a seasonal dip, such as a quiet May-June, from being read as a downtrend.
3. **Trend:** a straight line through the last `trend_lookback_days` of the deseasonalised series, damped by 0.9 per week so it levels off after about two months instead of running for a full year. The level is calibrated so the model reproduces the call total of the last 28 training days.
4. **Holidays:** observed US federal holidays plus Thanksgiving eve, the day after Thanksgiving, Christmas Eve and New Year's Eve ([`calendar.py`](app/core/forecasting/calendar.py)). Each holiday's multiplier is learned from its past occurrences and blended with a class default (closure 0.15, reduced 0.6, pre-holiday peak 1.6, normal 1.0).
5. **Intervals:** each forecast day is split with the weekday's intraday profile from the last 26 weeks (a pooled holiday profile on low-volume holidays). Interval counts are rounded with largest remainders so they add up to the daily total.

`decomposition_summary` then reports `annual_harmonics`, `annual_peak_to_trough_ratio`, `trend_knots_days`, `trend_slope_per_day_log`, `trend_damping_per_week`, `terminal_level_calls_per_day`, `level_calibration_factor`, `weekly_factors`, `holiday_days_adjusted`, `holiday_multipliers`, `outliers_masked`, `outlier_dates`, `residual_std_log`, and `predicted_daily_mean`.

One-off events (outages, storms) are masked when learning the pattern and are not predicted. The forecast calendar rules below are unchanged: with history ending on 30 September, the automatic yearly forecast covers the next calendar year starting 1 January, so pass an explicit `forecast_days` to forecast October onward.

### Short-history rolling profiles

The fallback averages earlier daily profiles, rounds interval counts, and adds predicted days to the profile history for later predictions. It uses all profiles with fewer than 7 available days; matching weekdays at 7-30; matching day-of-month/weekday at 31-365; and matching month/day/weekday thereafter, with progressively broader fallbacks. The longer-history rules apply only when a custom STL period is too large for two cycles. Missing slots on recorded dates have zero calls; dates with no valid calls do not initialize profiles.

### Forecast calendar

With `forecast_days=365` (the dashboard default), the inclusive date span from the earliest to latest valid record selects the period:

| Historical date span | Prediction period |
|---|---|
| 1-6 days | Next day |
| 7-27 days | Next 7 days |
| 28 days to less than 12 calendar months | Next full calendar month |
| 12 calendar months or more | Next full calendar year |

Missing dates within the span count toward its duration. `historical_days` instead counts distinct dates with valid calls, so it can be smaller. Monthly/yearly forecasts begin on the first day of the following calendar month/year. For example, August 1-31, 2026 predicts September 1-30, 2026.

Non-default API durations start immediately after the latest input date and use the requested number of days, except spans under seven days always predict one day. Both `days` and `parameters.forecast_days` report the effective duration. `output_year` is the year of the first forecast date. An automatic annual forecast includes 366 days for a leap year (17,568 half-hour intervals), otherwise 365 days (17,520 intervals).

### Erlang C staffing

```text
traffic_erlangs = call_volume * aht_seconds / interval_seconds
```

`pyworkforce.queuing.ErlangC` calculates raw positions, scheduled positions after shrinkage, service level, waiting probability, and occupancy. Average speed of answer is calculated from waiting probability, AHT, raw positions, and traffic. Staffing calls are cached using call volume, AHT rounded to two decimals, interval duration, targets, shrinkage, and the agent limit.

Zero-call intervals return zero traffic/agents, 100% service level, zero waiting probability, zero occupancy, and zero ASA. Validation still applies to the request settings.

### Forecast response

The response contains a `forecast` array, `charts`, `parameters`, and summary fields such as `dataset_count`, `historical_years`, `output_year`, `days`, `forecast_interval_count`, `total_predicted_calls`, maximum staffing, peak interval, source-file cleaning counts, and prediction metadata. `decomposition_summary` reports the method applied, full-grid and observed interval counts, zero-filled intervals, historical span, seasonal period, and prediction start. STL also reports trend endpoints/slope, actual trend lookback, seasonal range, residual standard deviation, and reconstruction error. Fallback responses report their reason instead of STL diagnostics.

The actual forecast row columns are:

```text
interval_start, date, day_of_year, month, day, weekday, weekday_name,
hour, minute, call_volume, aht_seconds, traffic_erlangs, raw_agents,
scheduled_agents, service_level_percent, probability_waiting_percent,
occupancy_percent, asa_seconds
```

`weekday` is 0 for Monday through 6 for Sunday. `day_of_year` is the calendar day-of-year. `interval_start` is serialized as `YYYY-MM-DDTHH:MM:SS` without a timezone suffix.

Summary service level, occupancy, and ASA are arithmetic means over forecast intervals. Summary AHT uses weights `max(call_volume, 1)`. These summary calculations differ from the call-weighted hourly table calculations below. Charts contain monthly and daily totals plus weekday and time-of-day averages; see [API documentation](api_documentation.md) for their response structure.

## API routes and background jobs

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` | Serve the dashboard, or service information if the HTML is absent |
| `GET` | `/health` | Return `{"status":"healthy","version":"4.3.0"}` |
| `POST` | `/api/v1/cdr/stl-forecast` | Return the complete forecast synchronously |
| `POST` | `/api/v1/cdr/stl-forecast/async` | Save uploads and submit a background forecast job |
| `POST` | `/api/v1/cdr/backtest` | Train before a split date, forecast the held-out days, and score accuracy |
| `GET` | `/api/v1/jobs/{job_id}` | Return progress, status, error, and completed result |
| `GET` | `/api/v1/jobs/{job_id}/stream` | Stream progress as server-sent events |
| `POST` | `/api/v1/schedule/monthly` | Generate a monthly roster from JSON forecast rows |

The dashboard uses the async forecast route and polls every 500 ms. Submission returns HTTP 200 with `job_id`, `status: queued`, `poll_url`, and `stream_url`. Job statuses are `queued`, `processing`, `completed`, and `failed`. Read `result.forecast` from a completed polling response; failed jobs expose an `error`. Polling responses have `result: null` until completion.

SSE events contain progress/status and `has_result`, not the full forecast. Retrieve the polling endpoint for the result after completion. Unknown jobs return HTTP 404.

Example async submission in a POSIX shell (`curl.exe` can be used with equivalent arguments in PowerShell):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/cdr/stl-forecast/async \
  -F "files=@calls_2025.csv" \
  -F "interval_minutes=30" \
  -F "forecast_days=365" \
  -F "seasonal_period=336" \
  -F "trend_lookback_days=90" \
  -F "target_seconds=20" \
  -F "target_service_level=80" \
  -F "shrinkage=30" \
  -F "include_forecast_rows=true"
```

## Monthly scheduling

Send JSON containing `forecast`, `year`, `month` (1-12), optional `agent_count`, and optional `shift_start_times` to `/api/v1/schedule/monthly`. Forecast rows must contain `interval_start` and `scheduled_agents`; use the original forecast response, not the aggregated CSV. `agent_count` is null/omitted for automatic estimation, or an integer from 1 to 10,000.

| Shift code | Dashboard label | Hours |
|---|---|---|
| `NIGHT` | `00:00-08:00` | 00:00-08:00 |
| `MORNING` | `08:00-16:00` | 08:00-16:00 |
| `EVENING` | `16:00-00:00` | 16:00-24:00 |

Each date/shift requires the maximum `scheduled_agents` from its forecast intervals. Headcount is estimated across Monday-Sunday weeks as the maximum of `ceil(weekly required shift slots / 5)` and peak total daily staffing. A supplied count below this estimate is rejected.

The generator assigns at most one shift per day, at most five working days per week, and at least eight hours between successive work shifts within the generated month. Candidates are ranked by weekly workdays, total assignments, shift-specific assignments, and agent ID. All remaining agent/date combinations become `OFF`. Agents are named `Agent 001`, `Agent 002`, etc.

The response has `schedule` rows and a `summary` with headcount, assignment totals, `coverage_shortage`, `coverage_ok`, and per-shift coverage. The headcount estimate does not guarantee the greedy assignment will fill every shift: inspect coverage fields even after HTTP 200. Automatic estimation of zero agents for a month with no staffing demand is rejected by the generator's positive-agent-count check.

Months are generated independently. Weekly limits and rest checks do not carry prior-month assignments into a new monthly generation. The CSV exports the displayed daily staffing totals.

### Manual shift times

Monthly Agent Schedule accepts three start times in shift order. Each shift lasts 8 hours; starts must be 8 hours apart around the clock (for example 06:00, 14:00, 22:00). End times are calculated automatically. Defaults remain 00:00, 08:00, 16:00. The monthly API accepts an optional `shift_start_times` list of three HH:MM strings.

An overnight shift belongs to its start date. Staffing uses the peak forecast requirement across that shift, including the following day's available intervals. At the forecast boundary only available intervals can be evaluated; the first date's early hours belong to the preceding date's overnight shift. Rest checks use actual shift times. Daily counts, schedule labels and daily scheduled-agent exports use the selected times.

## Dashboard charts

1. **Monthly predicted calls:** each bar sums forecast calls for that month. Clicking a month highlights it, selects Week 1, and updates the weekday/time charts. Initial rendering selects the week containing the first forecast date when available.
2. **Average daily calls by weekday, selected week:** each Monday-Sunday bar is that date's total calls across all intervals. For example, 55 calls over 48 intervals displays **55**, not 1.15.
3. **Calendar weeks:** Week 1 begins on the first of the month and ends on Sunday. Later weeks run Monday-Sunday. Days outside the month or without forecast data stay blank. An actual zero-call day has value zero and no visible positive-height bar.
4. **All weeks:** each weekday bar is total calls on that weekday divided by the number of available forecast dates for that weekday in the month. Zero-call dates count; missing dates do not.
5. **Day selection:** clicking a weekday bar in a specific week highlights that date and displays its interval predictions in the time chart. Hovering shows its date and daily total. All-weeks mode shows average calls/day and does not select individual dates.
6. **Average calls by time:** shows each time slot averaged over the selected week's dates, or the month's dates in All mode. Selecting a date shows its individual interval values. Changing month/week clears the date selection. If no interval rows are available for the selection, the current implementation falls back to the API's overall time-of-day averages.
7. **Repeated bars:** different weeks can have identical daily totals when the fitted trend is flat, rounding removes small differences, or rolling fallback repeats a pattern. Week selection changes dates; it does not recalculate the forecast.

The weekday dashboard uses daily totals from `charts.daily` and forecast rows. The API's legacy `charts.weekday.average_calls` still means calls **per interval** across the whole forecast; it is not used for the dashboard's daily-call bars.

Charts assume a single forecast year. The API accepts longer horizons, but monthly aggregates combine the same month across years, and calendar-week navigation uses the first forecast year. The table and forecast CSV include only the output year. Use API rows directly for multi-year analysis.

## Dashboard filters and CSV exports

### Forecast table and CSV

The table shows a selected date and display interval. Calls are summed; AHT and service level are call-weighted averages, with arithmetic-average fallback for zero-call groups. Agent counts show peak source-interval requirements. AHT and service level have two decimal places.

**Download Forecast CSV** exports the available output year at a fixed 1-hour interval, independent of the selected date/display interval. The filename is `stl_erlang_forecast_<year>_hourly.csv`. It uses exactly these table columns:

```csv
Interval,Calls,AHT,Raw agents,Scheduled agents,Service level %
2026-01-01 00:00 - 01:00,40,175.00,4,5,87.50
```

A complete annual forecast produces 8,760 CSV data rows, or 8,784 in a leap year. Hour labels use forecast wall-clock dates, and the final hour ends at `24:00`. API JSON retains the original interval rows and additional staffing fields.

### Daily scheduled agents

The dashboard shows scheduled-agent counts for forecast dates in the selected month, grouped by the three 8-hour shifts. The API roster also contains OFF rows for remaining calendar dates. Only working assignments count. The dashboard displays daily staffing totals rather than an agent-level roster.

### Daily scheduled agents CSV

**Download Daily Scheduled Agents CSV** exports the exact displayed **Daily Scheduled Agents** table as `daily_scheduled_agents_<year>-<month>.csv`. Its four columns are Date and the three shift headings, including the generated custom time ranges. Each row contains the displayed date and working-agent counts in the same order, including zero counts.

The export reads the rendered table directly and requires no additional API requests. Changing form fields alone does not change the table or export; generate a schedule to apply new settings.

Both exports are built in the browser as UTF-8 CSV with BOM and CRLF row endings. Commas, quotes, and line breaks are escaped.

## Configuration, state, and logging

[app/config.py](app/config.py) defines the current limits as Python constants. The only environment-variable setting is `CORS_ALLOW_ORIGINS`, a comma-separated list of browser origins allowed to call the API. It is unset (CORS disabled) unless you set it, and [compose.yaml](compose.yaml) sets it for the backtest page; see [What's inside the container](#whats-inside-the-container).

| Constant | Value | Scope |
|---|---|---|
| `MAX_UPLOAD_BYTES` | `25 * 1024 * 1024` | Maximum bytes per uploaded file (25 MiB) |
| `MAX_UPLOAD_FILES` | `10` | Files per forecast submission |
| `MAX_FORECAST_DAYS` | `3650` | Maximum requested forecast horizon |
| `MAX_AGENT_COUNT` | `10000` | Request limit for interval staffing cap and monthly headcount |
| `MAX_FORECAST_ROWS` | `40000` | Maximum forecast rows in monthly requests |
| `MAX_WORKER_THREADS` | `8` | Background forecast thread-pool size |


Job status/results live in process memory and are lost on restart. Separate server processes do not share jobs; the current background manager is a local thread pool. Forecast and schedule state are not stored in a database. The browser keeps the current monthly schedule until a new forecast starts or the page is reloaded.

Requests are logged with method, path, status, duration, and an ID returned in `X-Request-ID`. Background workers log progress and errors. Logs go to the console and `logs/app_<date>.log` using daily rotation configured with `backupCount=30`. Temporary upload files are removed after processing.

## Errors and validation

| Status / result | Meaning |
|---|---|
| HTTP 400 | Data or calculation errors, such as unsupported files, invalid forecast settings, or insufficient headcount |
| HTTP 404 | Unknown background job |
| HTTP 413 | Uploaded file exceeds the size limit |
| HTTP 422 | FastAPI/Pydantic request type, required-field, or declared model-bound validation |
| HTTP 500 | Unexpected operation failure |
| Job `status: failed` | Background forecast failed after submission; inspect its `error` |
| `coverage_ok: false` | Monthly roster returned with a staffing shortage |

HTTP errors generally contain a `detail` field. Background completion and schedule coverage must be checked separately from the initial HTTP status.

## Development checks

Compile application modules:

```bash
python -m compileall app api.py
```
