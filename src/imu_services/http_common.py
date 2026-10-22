import hmac
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout, TooManyRequests
import psycopg
from prometheus_client import CollectorRegistry, Counter, Histogram
from .store import Store, Conflict, NotFound, Overloaded


class BodyLimit:
    """Limit the actual received body, including chunked requests."""
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/v2/runs/whole-record" and scope.get("method") == "POST":
            # This route owns streaming byte, decompression, time and concurrency limits.
            return await self.app(scope, receive, send)
        size, messages = 0, []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                response = JSONResponse({"error": "BODY_TOO_LARGE"}, status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()
        return await self.app(scope, replay, send)


def make_app(settings, title, store=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store or Store(settings)
        if not app.state.store.ready():
            raise RuntimeError("unsupported database schema")
        yield
        if store is None:
            app.state.store.close()

    app = FastAPI(title=title, version="1.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    registry = CollectorRegistry()
    requests = Counter("imu_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=registry)
    duration = Histogram("imu_http_duration_seconds", "HTTP request seconds", ["route"], registry=registry)
    app.state.metrics = registry

    @app.middleware("http")
    async def observe(request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        requests.labels(request.method, route, response.status_code).inc()
        duration.labels(route).observe(time.monotonic()-started)
        return response

    def auth(authorization: str | None = Header(default=None)):
        expected = "Bearer "+settings.api_token
        if not settings.api_token or authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid service credential",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"error": "CONFLICT", "detail": str(exc)}, status_code=409)

    @app.exception_handler(NotFound)
    async def missing(request, exc):
        return JSONResponse({"error": "NOT_FOUND"}, status_code=404)

    @app.exception_handler(Overloaded)
    @app.exception_handler(PoolTimeout)
    @app.exception_handler(TooManyRequests)
    @app.exception_handler(psycopg.errors.LockNotAvailable)
    async def overloaded(request, exc):
        return JSONResponse({"error": "RETRY_LATER"}, status_code=429, headers={"Retry-After": "5"})

    @app.exception_handler(psycopg.OperationalError)
    async def unavailable(request, exc):
        return JSONResponse({"error": "DATABASE_UNAVAILABLE"}, status_code=503, headers={"Retry-After": "5"})

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"error": "INVALID_INPUT", "detail": str(exc)}, status_code=422)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            if app.state.store.ready():
                return {"status": "ready"}
        except Exception:
            logging.getLogger(__name__).warning("database readiness failed")
        return JSONResponse({"status": "unavailable"}, status_code=503)

    return app, auth
