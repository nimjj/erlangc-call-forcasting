from __future__ import annotations

import time
import uuid
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR
from app.api.health import router as health_router
from app.api.v1.router import router as v1_router
from app.logger import logger

def create_app() -> FastAPI:
    application = FastAPI(
        title="CDR STL Forecast & Agent Scheduling API",
        description=(
            "Contact-centre demand forecasting using STL with short-history rolling profiles, Erlang C staffing, "
            "and monthly scheduling with three configurable 8-hour shifts."
        ),
        version="4.1.0",
    )

    @application.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        start_time = time.time()
        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path

        logger.info(f"[{request_id}] --> {method} {path} | Client: {client_ip}")

        try:
            response = await call_next(request)
            duration = (time.time() - start_time) * 1000
            status_code = response.status_code
            logger.info(f"[{request_id}] <-- {method} {path} | Status: {status_code} | Time: {duration:.2f}ms")
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as exc:
            duration = (time.time() - start_time) * 1000
            logger.exception(f"[{request_id}] <-- {method} {path} | 500 FAILED | Time: {duration:.2f}ms | Error: {exc}")
            raise

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    application.include_router(health_router)
    application.include_router(v1_router)

    logger.info("FastAPI Application initialized with daily rotating logging.")
    return application

app = create_app()
