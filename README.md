# CDR STL Forecast & Agent Scheduling

FastAPI application for forecasting contact-centre call demand from historical CDR files, calculating Erlang C staffing, and generating monthly agent rosters.

**Application version:** `4.1.0`

**Local dashboard:** `http://127.0.0.1:8000/`

The browser submits a background forecast job, displays progress and charts, builds monthly schedules, displays daily scheduled-agent counts, and supports yearly CSV downloads. [API documentation](api_documentation.md) contains endpoint examples and response details.

## Quick start

Use Python 3.10 or later. Dependencies are listed in [requirements.txt](requirements.txt): pandas, NumPy, pyworkforce, FastAPI, Uvicorn, and python-multipart. There is no frontend build step.

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
| `/` | Dashboard |
| `/health` | Health and application version |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | Generated OpenAPI schema |

The dashboard loads Chart.js from jsDelivr. The application creates `static/` and `logs/` when needed and requires write access for logs and temporary uploads.

## Project structure

```text
Calculator-Erlang-C/
|-- api.py                         # Compatibility entry point
|-- calculator.py                  # Compatibility exports from app.core
|-- app/
|   |-- main.py                    # App setup, routers, static files, request logging
|   |-- config.py                  # Paths and application limits
|   |-- logger.py                  # Console and daily rotating file logs
|   |-- api/
|   |   |-- health.py              # Dashboard and health routes
|   |   `-- v1/                    # Forecast, schedule, and job routes
|   |-- schemas/                   # Pydantic models; routes define actual responses
|   |-- core/
|   |   |-- constants.py           # CDR fields, forecast fields, shift definitions
|   |   |-- ingestion/             # CDR parsing, cleaning, interval construction
|   |   |-- forecasting/           # STL forecast and dashboard aggregates
|   |   |-- queuing/               # Erlang C calculations and staffing cache
|   |   `-- scheduling/           # Monthly rosters and rest validation
|   `-- workers/                   # In-memory jobs and background forecast worker
|-- static/index.html              # Dashboard, filters, and CSV generation
|-- requirements.txt
|-- api_documentation.md
|-- Forecast_Comparison.ipynb       # Forecast exploration notebook
|-- refactor.md                    # Refactoring notes
```

## Dashboard workflow

1. Upload one or more historical CDR files containing at least one day of valid records.
2. Set **Target answer seconds** and **Target service level %**, then select **Create STL forecast**.
3. Wait for the background job. Review summaries and monthly, weekday, and time-of-day charts.
4. Select a date and table interval to inspect staffing. Download the hourly forecast for the whole output year.
5. Select a schedule month and optionally enter **Available agent count**. Leave it blank for automatic minimum-headcount estimation, then select **Generate Monthly Schedule**.
6. Review the staffing summary and daily scheduled-agent counts for each shift.
7. Select **Download Daily Scheduled Agents CSV** to export the displayed daily staffing table with its current shift times and counts.

## STL inputs

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
| `forecast_days` | `365` | Full output year; expands to 366 days for a leap year |
| `seasonal_period` | `336` | One week: 48 intervals/day multiplied by 7 |
| `trend_lookback_days` | `90` | Recent trend window for linear extrapolation |
| `shrinkage` | `30` | 30% shrinkage |
| `include_forecast_rows` | `true` | Return rows needed by the tables and scheduler |
| `max_agents` | Not sent; API default `1000` | Maximum permitted raw or scheduled staffing per interval |

Changing the forecast table's display interval does not change STL inputs or rerun Erlang C. The table defaults to **1 hr** and supports **0.5, 1, 2, 4, and 8 hr** groupings. The forecast CSV always uses 1-hour groups.

### Inputs accepted by the forecast API

Both `POST /api/v1/cdr/stl-forecast` and `POST /api/v1/cdr/stl-forecast/async` accept `multipart/form-data` with these fields:

| Field | Type | Default | Validation / meaning |
|---|---|---|---|
| `files` | Repeated uploaded file | Required | 1-20 non-empty `.csv`/`.txt` files; at most 100 MiB per file |
| `interval_minutes` | Integer | `30` | Positive exact divisor of 1440 |
| `forecast_days` | Integer | `365` | 1-3650; `365` represents the full output year, including leap day |
| `seasonal_period` | Integer or omitted | Automatic | Seasonal cycle length in intervals; effective period must be at least 2 |
| `trend_lookback_days` | Integer | `90` | At least 7; uses at most the available historical trend |
| `target_seconds` | Number | `20` | Non-negative Erlang C answer-time target in seconds |
| `target_service_level` | Number | `80` | Converted fraction must be strictly between 0 and 1 |
| `shrinkage` | Number | `30` | Converted fraction must be at least 0 and less than 1 |
| `max_agents` | Integer | `1000` | 1-10,000; fails if either raw or scheduled positions exceed this value |
| `include_forecast_rows` | Boolean | `true` | `false` returns summaries/charts with an empty `forecast` array |

Percentage conversion divides values greater than 1 by 100; values at or below 1 are interpreted as fractions. Thus `80` and `0.8` both mean 80%, and `30` and `0.3` both mean 30% shrinkage. Exactly `1` means 100% and is rejected for both service level and shrinkage. Submit numeric form values, without a `%` suffix.

Omitting `seasonal_period` uses `(1440 // interval_minutes) * 7`. The current implementation also treats `0` as automatic. A nonzero supplied value must resolve to at least 2 intervals, and the historical series must contain at least two such cycles.

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
| `Agent`, `Wait time`, `Ringing time`, `Entry`, `Exit`, `Transferred`, `Dumped`, `Ended` | Retained as source context where present; `Talk time` determines answered calls |

For this format, rows with a positive `Talk time` are treated as answered calls.

### Canonical headerless format

```text
source,destination,call_datetime,duration,disposition,unique_id,caller_id
```

The reader tries UTF-16 with tabs, UTF-8 with BOM and commas, then Latin-1 with commas. It first uses pandas' C parser and falls back to its Python parser on a parsing error.

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

### Historical series and rolling prediction

The application combines all cleaned records into fixed intervals without requiring complete calendar years. Forecasting starts on the day after the latest available record.

Future calls are calculated recursively. With fewer than seven recorded calendar days, the system predicts only the next day; for example, Aug 30 and Aug 31 can be used to predict Sep 1, but Sep 2 is not predicted until more call-detail days are available. After seven recorded days, the first week uses expanding prior-day profiles, later days use prior weekly history, and the first year expands to monthly and then prior-year same-period profiles. New records are included in the available history whenever they are supplied.

### Forecast calendar

`output_year` is the year containing the first forecast day. Forecasts start on the day after the latest available record and contain the requested number of consecutive days.

For an annual leap-year forecast, `days` is `366`, while `parameters.forecast_days` retains the requested `365`. At 30-minute intervals, annual output contains 17,520 rows in an ordinary year or 17,568 in a leap year. Historical leap-day removal does not apply to forecast dates. Regenerate older forecasts to include leap day.

### Erlang C staffing

```text
traffic_erlangs = call_volume * aht_seconds / interval_seconds
```

`pyworkforce.queuing.ErlangC` calculates raw positions, scheduled positions after shrinkage, service level, waiting probability, and occupancy. Average speed of answer is calculated from waiting probability, AHT, raw positions, and traffic. Staffing calls are cached using call volume, AHT rounded to two decimals, interval duration, targets, shrinkage, and the agent limit.

Zero-call intervals return zero traffic/agents, 100% service level, zero waiting probability, zero occupancy, and zero ASA. Validation still applies to the request settings.

### Forecast response

The response contains a `forecast` array, `charts`, `parameters`, and summary fields such as `dataset_count`, `historical_years`, `output_year`, `days`, `forecast_interval_count`, `total_predicted_calls`, maximum staffing, peak interval, source-file cleaning counts, and decomposition statistics.

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
| `GET` | `/health` | Return `{"status":"healthy","version":"4.1.0"}` |
| `POST` | `/api/v1/cdr/stl-forecast` | Return the complete forecast synchronously |
| `POST` | `/api/v1/cdr/stl-forecast/async` | Save uploads and submit a background forecast job |
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

Send JSON containing `forecast`, `year`, `month` (1-12), and optional `agent_count` to `/api/v1/schedule/monthly`. Forecast rows must contain `interval_start` and `scheduled_agents`; use the original forecast response, not the aggregated CSV. `agent_count` is null/omitted for automatic estimation, or an integer from 1 to 10,000.

| Shift code | Dashboard label | Hours |
|---|---|---|
| `NIGHT` | `00:00-08:00` | 00:00-08:00 |
| `MORNING` | `08:00-16:00` | 08:00-16:00 |
| `EVENING` | `16:00-00:00` | 16:00-24:00 |

Each date/shift requires the maximum `scheduled_agents` from its forecast intervals. Headcount is estimated across Monday-Sunday weeks as the maximum of `ceil(weekly required shift slots / 5)` and peak total daily staffing. A supplied count below this estimate is rejected.

The generator assigns at most one shift per day, at most five working days per week, and at least eight hours between successive work shifts within the generated month. Candidates are ranked by weekly workdays, total assignments, shift-specific assignments, and agent ID. All remaining agent/date combinations become `OFF`. Agents are named `Agent 001`, `Agent 002`, etc.

The response has `schedule` rows and a `summary` with headcount, assignment totals, `coverage_shortage`, `coverage_ok`, and per-shift coverage. The headcount estimate does not guarantee the greedy assignment will fill every shift: inspect coverage fields even after HTTP 200. Automatic estimation of zero agents for a month with no staffing demand is rejected by the generator's positive-agent-count check.

Months are generated independently. Weekly limits and rest checks do not carry prior-month assignments into a new monthly generation. The CSV exports the displayed daily staffing totals.

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

The dashboard shows scheduled-agent counts for every day of the selected month, grouped by the three 8-hour shifts. Only working assignments count. The dashboard displays daily staffing totals rather than an agent-level roster.

### Daily scheduled agents CSV

**Download Daily Scheduled Agents CSV** exports the exact displayed **Daily Scheduled Agents** table as `daily_scheduled_agents_<year>-<month>.csv`. Its four columns are Date and the three shift headings, including the generated custom time ranges. Each row contains the displayed date and working-agent counts in the same order, including zero counts.

The export reads the rendered table directly and requires no additional API requests. Changing form fields alone does not change the table or export; generate a schedule to apply new settings.

Both exports are built in the browser as UTF-8 CSV with BOM and CRLF row endings. Commas, quotes, and line breaks are escaped.

## Configuration, state, and logging

[app/config.py](app/config.py) defines the current limits; these values are Python constants, not environment-variable settings.

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
| HTTP 400 | Data or calculation errors, such as duplicate historical years, unsupported files, invalid forecast settings, insufficient headcount |
| HTTP 404 | Unknown background job |
| HTTP 413 | Uploaded file exceeds the size limit |
| HTTP 422 | FastAPI/Pydantic request type, required-field, or declared model-bound validation |
| HTTP 500 | Unexpected operation failure |
| Job `status: failed` | Background forecast failed after submission; inspect its `error` |
| `coverage_ok: false` | Monthly roster returned with a staffing shortage |
| Leave `resolved: false` | Leave result is unresolved; inspect its method and coverage |

HTTP errors generally contain a `detail` field. Background completion and schedule coverage must be checked separately from the initial HTTP status.

## Development checks

Compile application modules:

```bash
python -m compileall app api.py calculator.py
```

If the local `tests/` directory is present, run its unittest checks:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

The current `.gitignore` excludes `tests/`, so local tests may not be available in a fresh clone. The notebook and refactoring notes are supporting material; runtime behaviour is implemented in `app/` and `static/index.html`.

### Automatic forecast periods

The default dashboard forecast uses the inclusive date span from the earliest to the latest valid record across all uploaded files. Days without calls inside that span still count toward its duration.

| Historical date span | Prediction period |
|---|---|
| 1-6 days | Next day |
| 7-27 days | Next 7 days |
| 28 days to less than 12 calendar months | Next full calendar month |
| 12 calendar months or more | Next full calendar year |

For example, August 1-31, 2026 predicts September 1-30, 2026. A 28-day period ending partway through August also predicts September. Monthly and yearly predictions begin on the first day of the following calendar month or year; leap years and variable month lengths are handled automatically.

Explicit non-default API forecast durations retain their requested length, except that inputs spanning fewer than seven days still predict only the next day. Both synchronous and asynchronous responses report the effective duration.

### Manual shift times

Monthly Agent Schedule accepts three start times in shift order. Each shift lasts 8 hours; starts must be 8 hours apart around the clock (for example 06:00, 14:00, 22:00). End times are calculated automatically. Defaults remain 00:00, 08:00, 16:00. The monthly API accepts an optional `shift_start_times` list of three HH:MM strings.

An overnight shift belongs to its start date. Staffing uses the peak forecast requirement across that shift, including the following day's available intervals. At the forecast boundary only available intervals can be evaluated; the first date's early hours belong to the preceding date's overnight shift. Rest checks use actual shift times. Daily counts, schedule labels and daily scheduled-agent exports use the selected times.
