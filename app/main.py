from __future__ import annotations

import hmac
import json
import time
import uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ALLOW_ORIGINS, INTERNAL_API_KEY, MAX_REQUEST_BYTES
from app.api.health import router as health_router
from app.api.v1.router import router as v1_router
from app.logger import logger

async def _send_json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    """Rejects oversized bodies while they stream in, and (when INTERNAL_API_KEY is set) /api calls without it.

    Starlette spools every uploaded part to disk before an endpoint runs, so the per-file checks in the
    endpoints came after the whole body had already been written to /tmp.
    """

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES, api_key: str = INTERNAL_API_KEY):
        self.app = app
        self.max_bytes = max_bytes
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if self.api_key and scope["path"].startswith("/api/"):
            supplied = headers.get(b"x-internal-key", b"").decode("latin-1")
            if not hmac.compare_digest(supplied, self.api_key):
                return await _send_json(send, 401, "Missing or wrong X-Internal-Key.")
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            return await _send_json(send, 413, f"Request body is larger than {self.max_bytes // (1024 * 1024)} MB.")

        received = 0

        async def limited_receive():
            # A body without Content-Length (chunked) is counted as it arrives. Raised while the endpoint
            # reads the body, so FastAPI answers 413 and stops reading.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(
                        status_code=413, detail=f"Request body is larger than {self.max_bytes // (1024 * 1024)} MB.",
                    )
            return message

        await self.app(scope, limited_receive, send)


def create_app() -> FastAPI:
    application = FastAPI(
        title="CDR STL Forecast & Agent Scheduling API",
        description=(
            "Contact-centre demand forecasting using STL with short-history rolling profiles, Erlang C staffing, "
            "and monthly scheduling with three configurable 8-hour shifts."
        ),
        version="4.4.0",
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

    if CORS_ALLOW_ORIGINS:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ALLOW_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    application.include_router(health_router)
    application.include_router(v1_router)
    # Outermost, so nothing reads the body before the limit applies.
    application.add_middleware(RequestGuard)

    logger.info("FastAPI Application initialized with daily rotating logging.")
    return application

app = create_app()
