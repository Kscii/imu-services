"""Read-only HTTP endpoints and the analysis composition root."""
from datetime import date
from contextlib import contextmanager
from threading import BoundedSemaphore
from uuid import UUID
from fastapi import Depends, Query
from .http_common import make_app
from .settings import Settings
from .models import ActivityAnalysisRequest
from .errors import Overloaded
from .readers import (ResultReader as ResultReader, PostgresReader as PostgresReader,
                      HttpResultReader as HttpResultReader)


def create_app(settings=None, store=None):
    settings = settings or Settings.from_env("query")
    app, auth = make_app(settings, "IMU query and analysis", store)
    secured = [Depends(auth)]
    app.state.analysis_slots = BoundedSemaphore(1)

    def reader():
        return PostgresReader(app.state.store)

    @contextmanager
    def analysis_slot():
        if not app.state.analysis_slots.acquire(blocking=False):
            raise Overloaded("an analysis report is already computing; retry later")
        try:
            yield
        finally:
            app.state.analysis_slots.release()

    @app.get("/v2/runs/{run_id}", dependencies=secured)
    def run(run_id: UUID):
        return reader().run(run_id)

    @app.get("/v2/runs/{run_id}/results", dependencies=secured)
    def results(run_id: UUID, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        rows = reader().results(run_id, after, limit)
        return {"items": rows, "next_after": rows[-1]["id"] if rows else after}

    @app.get("/v2/recordings/{recording_id}/versions", dependencies=secured)
    def versions(recording_id: str, limit: int = Query(100, ge=1, le=500)):
        return {"items": reader().versions(recording_id, limit)}

    @app.get("/v2/runs/{run_id}/analysis", dependencies=secured)
    def analysis(run_id: UUID):
        from .analysis import recording_summary
        return recording_summary(reader(), run_id)

    @app.get("/v2/runs/{run_id}/calendar", dependencies=secured)
    def calendar(run_id: UUID, timezone: str, date_from: date, date_to: date):
        from .analysis import calendar_summary
        with analysis_slot():
            return calendar_summary(reader(), run_id, timezone, date_from, date_to)

    @app.post("/v2/analysis/activity", dependencies=secured)
    def activity_report(request: ActivityAnalysisRequest):
        from .analysis import activity_analysis
        with analysis_slot():
            return activity_analysis(reader(), request)

    @app.get("/v2/operations", dependencies=secured)
    def operations():
        with app.state.store.pool.connection() as c:
            queue = c.execute("SELECT * FROM query_v1_queue").fetchall()
            budget = c.execute("SELECT * FROM query_v1_budget").fetchone()
            workers = c.execute("SELECT * FROM query_v1_workers").fetchall()
            modes = c.execute("SELECT * FROM query_v2_modes").fetchall()
        return {"queue": {r["status"]: r["runs"] for r in queue}, "budget": budget,
                "workers": workers, "modes": modes}

    @app.get("/metrics", dependencies=secured)
    def metrics():
        from fastapi.responses import Response
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return Response(generate_latest(app.state.metrics), media_type=CONTENT_TYPE_LATEST)

    return app
