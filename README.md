# CDR STL Forecast & Agent Scheduling

A REST API that reads historical call-detail records (CDRs) from a contact centre, forecasts call volume, works out how many agents are needed, and builds a monthly roster of three 8-hour shifts. It ships as a Docker container that exposes the API on port 8000.

**Version:** `4.4.0`

## What it does

1. **Forecasts calls** per half-hour from CDR files. With a year or more of history it learns weekly and yearly patterns and US holidays (STL decomposition); with less it falls back to simpler models and says so in the response.
2. **Sizes staffing** with Erlang C: the agents needed in each half-hour to answer a target share of calls within a target time, plus a shrinkage margin for breaks and absence.
3. **Plans shifts.** Each shift is sized to the busiest half-hour it is likely to see, so that about 80% of shifts are fully covered. Night and weekend shifts get a minimum of 3 agents.
4. **Builds a monthly roster** for those shifts.
5. **Checks itself.** A backtest endpoint and command-line tools measure how well the forecast and the shift plan match what really happened.

The call forecast is typically off by about a fifth on any single day and by 5-6% over a month, and the shift plan covered about 93% of shifts in testing. Read [LOGIC_AND_LIMITATIONS.md](LOGIC_AND_LIMITATIONS.md) before relying on the numbers.

### What is new in 4.4.0

- **Shift sizing from a simulation** instead of the plain forecast: shifts covered rose from 55% to 93% in testing (51% to 94% on rolling monthly tests), at the cost of about 42% more agent-shifts than the real calls strictly needed. New schedule fields: `coverage_percent`, `night_minimum_agents`, `weekend_minimum_agents` (see [ARCHITECTURE.md](ARCHITECTURE.md#73-monthly-schedule)). The old behaviour is available with `coverage_percent: null` and both minimums `0`.
- Small tuning of the yearly forecast model (about 3% lower daily error, smaller bias).
- Container hardening and a smoke test (`tools/smoke_test.py`); a forecast-vs-real drill-down page with an agents-per-shift view; full documentation.

## Quick start with Docker

**You need** Docker with the Compose plugin: Docker Desktop on Windows or macOS, or Docker Engine on Linux. On Windows (including Home) Docker Desktop runs on WSL 2; if Docker Desktop will not start, run `wsl --install` once in an administrator terminal and restart.

```powershell
git clone https://github.com/nimjj/erlangc-call-forcasting.git
cd erlangc-call-forcasting
docker compose up --build -d
```

Check that it is running (Docker marks the container healthy within about 30 seconds):

```powershell
curl.exe http://127.0.0.1:8000/health
# {"status":"healthy","version":"4.4.0"}
python tools/smoke_test.py
# 8 of 8 checks passed
```

Then open **http://127.0.0.1:8000/docs** for the interactive API. Opening `/` returns a short JSON message, because the container has no browser dashboard.

| Task | Command |
|---|---|
| Follow the logs | `docker compose logs -f app` |
| Stop | `docker compose down` |
| Stop and delete the log volume | `docker compose down -v` |
| Update after pulling new code | `docker compose up --build -d` |
| Use another port | set `APP_PORT=8080` in a `.env` file (copy `.env.example`), then `docker compose up -d` |

`python tools/smoke_test.py` needs only Python 3 (no packages). Use `--base-url` if you changed the port.

## Your first forecast and schedule

Send your own CDR files (see [accepted formats](ARCHITECTURE.md#61-cdr-input)). With one year or more of history and `forecast_days=365`, the service forecasts the next calendar year.

```bash
# 1. Forecast
curl -X POST http://127.0.0.1:8000/api/v1/cdr/stl-forecast \
  -F "files=@calls.csv" -F "interval_minutes=30" \
  -F "target_seconds=20" -F "target_service_level=80" -F "shrinkage=30" > forecast.json

# 2. Turn one month of that forecast into a roster
python - <<'EOF' > schedule-request.json
import json
rows = json.load(open("forecast.json"))["forecast"]
print(json.dumps({"forecast": rows, "year": int(rows[0]["interval_start"][:4]), "month": int(rows[0]["interval_start"][5:7])}))
EOF
curl -X POST http://127.0.0.1:8000/api/v1/schedule/monthly \
  -H "Content-Type: application/json" --data-binary "@schedule-request.json"
```

In PowerShell use `curl.exe`. For large histories use the background route (`/api/v1/cdr/stl-forecast/async`, then poll `/api/v1/jobs/{job_id}`). Every endpoint, field and response is described in [ARCHITECTURE.md](ARCHITECTURE.md#7-endpoint-reference) and [api_documentation.md](api_documentation.md).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_PORT` | `8000` | Port published on `127.0.0.1` |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5500,http://127.0.0.1:5500` | Browser origins allowed to call the API. Set it to your front end's address, or leave it empty to block browsers on other origins |
| `APP_MEM_LIMIT` | `2g` | Container memory limit |

Put them in a `.env` file next to `compose.yaml` (start from `.env.example`). Staffing settings (service target, shrinkage, coverage, minimums) are request fields, not environment variables. Upload limits are in `app/config.py`: 10 files of 25 MiB, 3,650 forecast days, 40,000 forecast rows per schedule request.

### What is in the container

| Item | Detail |
|---|---|
| Image | `python:3.12-slim`, containing only `app/` and the pinned `requirements.txt` |
| Process | `uvicorn app.main:app`, one worker, port 8000 |
| User and filesystem | Non-root (UID 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges` |
| Limits | 2 GB memory, 256 processes, 512 MiB temporary upload space (deleted after each request) |
| Logs | Console (rotated by Docker: 5 files of 10 MB) and `/app/logs` in the `app-logs` volume (daily files, 30 kept) |
| Health check | `GET /health` every 30 seconds |
| Not included | The browser pages (`static/`, `backtest-ui/`), `tools/`, test data and documentation |

## Using it in production

The service is set up to run unattended, but it is deliberately simple. Before exposing it:

- **Add authentication and HTTPS.** The API has none. The container publishes on `127.0.0.1` only; put a reverse proxy with authentication and TLS in front of it if other machines need to reach it.
- **Keep one worker and one container.** Background jobs and results are held in the memory of that process and are lost on restart. Fetch results promptly.
- **Set `CORS_ALLOW_ORIGINS`** to the real front-end origin.
- **Size memory** to your uploads (`APP_MEM_LIMIT`). A yearly forecast from a year of data takes seconds to a minute on a laptop.
- **Watch** `/health` and `docker compose logs`. Every request is logged with an ID that is also returned in the `X-Request-ID` header.
- **Validate on your own data** before using the numbers to roster people (see below), and re-check as new history arrives.

## Run without Docker

Python 3.10 or later. This also serves the browser dashboard.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

On Linux or macOS use `source .venv/bin/activate`. Then open http://127.0.0.1:8000/ for the dashboard and `/docs` for the API.

## Checking quality

| Tool | What it tells you |
|---|---|
| `python tools/smoke_test.py` | A running API works end to end |
| Backtest page: `python -m http.server 5500 --directory backtest-ui`, then http://localhost:5500 | How close the forecast is to held-out days of your data. `drilldown.html` zooms from month to half-hour |
| `python tools/shift_coverage_check.py --data <file>` | Share of shifts covered, short and extra, old rule vs current policy (needs a CDR file with earlier history plus a test period) |
| `python tools/build_comparison_page.py` | One HTML page comparing the forecast with the real calls, and planned with needed agents per shift, drillable month to half-hour |

The tools that read data expect it under `testdata/`, which is not in the repository.

## Documentation

| Document | Read it for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture, code layout, request flows, every endpoint, data formats, configuration |
| [LOGIC_AND_LIMITATIONS.md](LOGIC_AND_LIMITATIONS.md) | The forecasting and staffing logic in plain language, simplified maths, measured error rates, and limitations |
| [api_documentation.md](api_documentation.md) | Field-by-field request and response reference with examples |
| `/docs` on a running service | Interactive API explorer |

## Accuracy and limits in brief

- Typical error: about 21% on a single day's calls, 5-6% on a month's, and large on a single half-hour (counts are tiny, so it is mostly chance).
- The queue modelled is small (a few calls per half-hour), where Erlang C steps sharply: one call needs 3 agents, six need 8. The staffing numbers are therefore generous on quiet shifts.
- Test accuracy came from data shaped like the training data, so expect somewhat worse on genuinely new data until you measure it.
- Only answered calls count as demand; one-off events are not predicted; there are no prediction intervals.
- No authentication, one worker, in-memory jobs, no time zone handling.

The full list, with what each limit means and what to do about it, is in [LOGIC_AND_LIMITATIONS.md](LOGIC_AND_LIMITATIONS.md#6-limitations).

## Development checks

```bash
python -m compileall app api.py        # syntax check
python tools/smoke_test.py             # end-to-end check of a running API
```

The repository layout is described in [ARCHITECTURE.md](ARCHITECTURE.md#3-repository-map).
