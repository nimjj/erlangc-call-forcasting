# Local Docker Deployment Plan

This guide builds, runs, and verifies the Python **CDR STL Forecast & Agent Scheduling API** (`erlangc-forecast` on port `:8010`) locally with Docker.

This service runs as a single container microservice that processes Call Detail Records (CDRs), generates STL forecasts, runs Erlang C staffing models, and produces monthly shift rosters. ciap-api calls this service on the internal network; the browser (ciap-web) goes through ciap-api and should not call it directly. Set `INTERNAL_API_KEY` here and in ciap-api. Postman and `/docs` are for local development.

---

## 1. Prerequisites & Directory Setup

Ensure Docker Desktop (or Docker Engine) is running and create the host directory for persistent application logs:

```bash
mkdir -p logs
```
*(On Windows PowerShell: `New-Item -ItemType Directory -Force logs`)*

---

## 2. Environment Configuration

Copy the example environment configuration:

```bash
cp .env.example .env
```
*(On Windows PowerShell: `Copy-Item .env.example .env`)*

Review your `.env` file. It configures host port `8010` and allows cross-origin requests from frontends on ports `8080` and `8081`:

```ini
APP_PORT=8010
CORS_ALLOW_ORIGINS=http://localhost:8080,http://127.0.0.1:8080,http://localhost:8081,http://127.0.0.1:8081,http://localhost:3000,http://localhost:5173
```

---

## 3. Build Docker Image

Build the service container:

```bash
docker build -t erlangc-forecast:v4.4.0 .
```

*Build details:*
- Uses `python:3.12-slim` base image.
- Installs pinned dependencies (`pandas`, `numpy`, `statsmodels`, `pyworkforce`, `fastapi`, `uvicorn`).
- Sets up an unprivileged non-root user (`appuser`).

---

## 4. Run the Container (Port 8010)

Start the container in detached mode, exposing port **`8010`**:

### Bash / Linux / macOS:
```bash
docker run -d \
  --name erlangc_api \
  -p 127.0.0.1:8010:8010 \
  --env-file .env \
  -v erlangc_logs:/app/logs \
  erlangc-forecast:v4.4.0
```

### Windows PowerShell:
```powershell
docker run -d `
  --name erlangc_api `
  -p 127.0.0.1:8010:8010 `
  --env-file .env `
  -v erlangc_logs:/app/logs `
  erlangc-forecast:v4.4.0
```

*(Note: If running directly in Python without Docker, use: `python -m uvicorn app.main:app --port 8010 --reload`)*

---

## 5. Monitor Container Logs

Follow the live logs to verify that the API server started:

```bash
docker logs -f erlangc_api
```

### Sample Expected Output:
```text
2026-10-30 04:45:00 | INFO    | [app] | FastAPI Application initialized with daily rotating logging.
2026-10-30 04:45:00 | INFO    | [uvicorn.error] | Started server process [1]
2026-10-30 04:45:00 | INFO    | [uvicorn.error] | Waiting for application startup.
2026-10-30 04:45:00 | INFO    | [uvicorn.error] | Application startup complete.
2026-10-30 04:45:00 | INFO    | [uvicorn.error] | Uvicorn running on http://0.0.0.0:8010 (Press CTRL+C to quit)
```

Press `Ctrl+C` to stop following the logs (the container keeps running in the background).

---

## 6. Health Check Verification

Verify that the service is running and healthy on port **8010**:

```bash
curl http://localhost:8010/health
```

### Expected Output:
```json
{"status":"healthy","version":"4.4.0"}
```

And verify the service info root:
```bash
curl http://localhost:8010/
```
```json
{"message":"CDR STL Forecast & Agent Scheduling API","documentation":"/docs"}
```

---

## 7. Smoke Test & Verification

Run the end-to-end smoke test against the running service on port **8010**:

```bash
python tools/smoke_test.py --base-url http://localhost:8010
```

### Expected Output:
```text
PASS  health  (version 4.4.0)
PASS  forecast  (HTTP 200, STL, 336 intervals, 247 calls)
PASS  forecast rows carry what the scheduler needs
PASS  background job completes  (status completed)
PASS  schedule with the default staffing policy  (HTTP 200, 97 agent-shifts required)
PASS  default policy needs at least as many agent-shifts as the plain forecast  (97 vs 60)
PASS  invalid coverage_percent is rejected  (HTTP 422)
PASS  unknown job returns 404  (HTTP 404)

8 of 8 checks passed
```

You can also run a manual forecast with a CDR export of your own (here `sample_calls.csv`):
```bash
curl -X POST http://localhost:8010/api/v1/cdr/stl-forecast \
  -F "files=@sample_calls.csv" \
  -F "interval_minutes=30" \
  -F "target_seconds=20" \
  -F "target_service_level=80" \
  -F "shrinkage=30" \
  -o forecast_output.json
```

---

## 8. Frontend Integration

1. **Interactive API Explorer**: Visit `http://localhost:8010/docs` in your browser.
2. **Postman Collection**: `postman_collection.json` is pre-configured with `baseUrl: http://127.0.0.1:8010`.
3. **CORS Support**: Frontends running on `http://localhost:8080`, `http://localhost:8081`, `http://localhost:3000`, or `http://localhost:5173` are pre-authorized in `.env`.

---

## 9. Teardown / Cleanup

Stop and remove the container when done:

```bash
docker stop erlangc_api
docker rm erlangc_api
```
