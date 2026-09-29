> Historical notes: these describe an earlier STL implementation. Current code uses rolling-profile forecasting. Leave/swap APIs, unused response schemas and placeholder Postman requests have since been removed. See README.md for current behavior.

# Refactoring Summary

A simple overview of what was done, the speed improvements, and what each file does.

---

## 1. What Was Done

1. **Decoupled Architecture**: Split the giant 2,600 line monolithic `calculator.py` file into a clean, modular `app/` folder structure.
2. **52x Speedup**: Fixed the 6 minute freeze. Changed STL decomposition to use Cleveland jumps. Processing 4 years of CDR data went from **6 minutes 11 seconds down to 7.17 seconds**.
3. **Asynchronous Processing**: Added background task execution. Large files upload instantly (< 50ms) and show a real time progress bar in the UI.
4. **Code Cleanup**: Removed hundreds of messy comments, divider banners, and extra blank lines.
5. **Daily Logging**: Added automatic daily log files inside a `logs/` folder (`app_YYYY-MM-DD.log`) with request IDs, client IPs, response times, and system errors.
6. **Backward Compatibility**: Kept `api.py` and `calculator.py` as simple entry points so all existing tests continue to work.
7. **Future Upgrade (Polars)**: We currently use Pandas with its fast native C engine (`engine="c"`). To optimize even further in the future, **`Polars`** (a multi-threaded Rust based data library) is recommended to replace Pandas for near instant CSV parsing and lower memory usage.

---

## 2. What Each File Does

### App Setup & Logging
- **`app/config.py`**: Stores file paths, upload limits, and settings.
- **`app/logger.py`**: Configures daily rotating log files that reset every midnight.
- **`app/main.py`**: Creates the FastAPI app and logs incoming HTTP requests with time taken and request ID.

### API Routes (`app/api/`)
- **`app/api/health.py`**: Serves `/health` check and the `/` web dashboard.
- **`app/api/v1/forecast.py`**: Handles CDR file upload and forecast generation (both sync and async).
- **`app/api/v1/jobs.py`**: Checks background job status and progress (`/api/v1/jobs/{job_id}`).
- **`app/api/v1/schedule.py`**: Handles monthly agent schedule, leave requests, and shift swaps.
- **`app/api/v1/router.py`**: Combines all v1 API routes together.

### Schemas (`app/schemas/`)
- **`app/schemas/forecast.py`**: Data validation models for forecasting inputs and outputs.
- **`app/schemas/schedule.py`**: Data validation models for schedule, leave, and swap requests.
- **`app/schemas/jobs.py`**: Data models for job status and progress reports.

### Core Business Logic (`app/core/`)

#### Ingestion (`app/core/ingestion/`)
- **`reader.py`**: Reads large CSV files quickly using C-engine and handles encodings.
- **`cleaner.py`**: Validates CDR records, parses durations, and filters invalid rows.
- **`interval.py`**: Groups raw call data into 30-minute time intervals.

#### Forecasting (`app/core/forecasting/`)
- **`stl.py`**: Runs STL decomposition and predicts next year's call volume and handling times (super-fast with Cleveland jumps).
- **`aggregates.py`**: Calculates daily, monthly, and hourly averages for dashboard charts.

#### Queuing & Erlang C (`app/core/queuing/`)
- **`erlang_c.py`**: Core Erlang C math for call traffic, wait probability, and agent count.
- **`staffing.py`**: Calculates raw and scheduled agents (with shrinkage) for all 17,520 intervals.

#### Scheduling (`app/core/scheduling/`)
- **`shifts.py`**: Converts 30-minute staffing into 3 daily 8-hour shifts (Morning, Evening, Night).
- **`roster.py`**: Generates a fair monthly agent schedule (max 5 working days per week).
- **`leave.py`**: Processes employee leave and finds safe replacements.
- **`swap.py`**: Allows two agents to swap shifts while enforcing an 8-hour minimum rest period.

### Background Workers (`app/workers/`)
- **`task_manager.py`**: Thread-safe manager that runs heavy jobs in the background.
- **`jobs.py`**: Background job logic for running the STL forecast and updating progress.

### Root Files & Frontend
- **`api.py`**: Main server entry point (runs `uvicorn api:app`).
- **`calculator.py`**: Compatibility facade that exports core functions to keep tests working.
- **`static/index.html`**: Web dashboard with charts and real-time progress bar.
- **`logs/`**: Folder containing daily log files (`app_YYYY-MM-DD.log`).
