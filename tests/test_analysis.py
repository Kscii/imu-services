from copy import deepcopy
from datetime import datetime
from uuid import UUID, uuid4
import subprocess
import sys

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from imu_inference import InputBatch
from imu_services.analysis import activity_analysis
from imu_services.errors import Conflict, NotFound
from imu_services.models import ActivityAnalysisRequest
from imu_services.query import create_app
from imu_services.readers import HttpResultReader, PostgresReader
from imu_services.worker import Worker
from test_integration import finish


class MemoryReader:
    def __init__(self):
        self.runs, self.rows = {}, {}

    def add(self, index, start, timeline, *, recording_id=None, fingerprint="fixed"):
        run_id = str(UUID(int=index))
        n = timeline[-1]["end"] if timeline else 0
        self.runs[run_id] = dict(
            status="completed", recording_id=recording_id or f"r{index}", device_id=f"d{index}",
            start_time=start, sealed_end=n, fingerprint=fingerprint, provenance={"fixture": "fixed"},
            summary={"finished": True, "steps": {"enabled": False, "estimated_step_count": None}},
        )
        self.rows[run_id] = [dict(
            id=index, run_id=run_id, final=True, content_sha256="a"*64,
            events=[{"kind": "activity_core", "start": 0, "end": n, "timeline": timeline}] if n else [])]
        return run_id

    def run(self, run_id):
        if str(run_id) not in self.runs:
            raise NotFound()
        return deepcopy(self.runs[str(run_id)])

    def results(self, run_id, after=0, limit=100):
        return deepcopy([r for r in self.rows[str(run_id)] if r["id"] > after][:limit])


def span(a, b, label="walking_running", status="observed"):
    return dict(start=a, end=b, label=label, status=status)


def request(ids=(), **kwargs):
    return ActivityAnalysisRequest(
        participant_id="anonymous-1", run_ids=list(ids),
        **({"timezone": "Australia/Sydney", "date_from": "2026-10-04", "date_to": "2026-10-04"} | kwargs))


def test_multi_record_dst_missing_excluded_and_stable_manifest():
    reader = MemoryReader()
    first = reader.add(1, "2026-10-03T23:59:30+10:00", [span(0, 1500)])
    second = reader.add(2, datetime.fromisoformat("2026-10-04T03:00:00+11:00"), [
        span(0, 250, "sitting"), span(250, 500, None, "invalid"), span(500, 750, None, "excluded")])
    got = activity_analysis(reader, request([second, first, first]))
    day = got["calendar"]["daily"][0]
    assert day["calendar_s"] == 23*3600
    assert len(got["calendar"]["hourly"]) == 23
    assert day["observed_s"] == 40 and day["invalid_s"] == 10 and day["excluded_s"] == 10
    assert day["duration_s"]["walking_running"] == 30 and day["duration_s"]["sitting"] == 10
    assert day["fraction_of_observed"]["sitting"] == .25
    assert day["unobserved_s"] == 23*3600-60
    assert got["duplicate_run_ids_removed"] == 1
    assert got["recording_steps"][0]["scope"] == "whole_recording_not_date_clipped"
    reordered = activity_analysis(reader, request([first, second]))
    assert got["source_manifest_sha256"] == reordered["source_manifest_sha256"]
    assert got["calendar"] == reordered["calendar"]
    assert got["sources"] == reordered["sources"]
    assert got["calendar"]["weekly"][0]["included_dates"] == ["2026-10-04"]
    assert "timeline" not in day and "strict_label_runs" not in got["calendar"]


def test_empty_and_dst_fall_day():
    got = activity_analysis(MemoryReader(), request(date_from="2026-04-05", date_to="2026-04-05"))
    day = got["calendar"]["daily"][0]
    assert day["state"] == "NO_OBSERVATIONS"
    assert day["duration_s"]["walking_running"] is None
    assert day["calendar_s"] == 25*3600 and len(got["calendar"]["hourly"]) == 25
    assert got["calendar"]["model_version"] is None and got["sources"] == []


def test_analysis_import_does_not_require_database_web_or_model_runtime():
    subprocess.run([sys.executable, "-c",
                    "import sys; import imu_services.analysis; "
                    "assert not {'psycopg', 'torch', 'fastapi'} & set(sys.modules)"], check=True)


@pytest.mark.parametrize("fault", ["unfinished", "revision", "fingerprint", "overlap", "missing_final"])
def test_conflicting_inputs_are_rejected(fault):
    reader = MemoryReader()
    a = reader.add(1, "2026-10-04T01:00:00+10:00", [span(0, 1500)])
    b = reader.add(2, "2026-10-04T01:01:00+10:00", [span(0, 1500)])
    if fault == "unfinished":
        reader.runs[b]["status"] = "running"
    elif fault == "revision":
        reader.runs[b]["recording_id"] = "r1"
    elif fault == "fingerprint":
        reader.runs[b]["fingerprint"] = "other"
    elif fault == "overlap":
        reader.runs[b]["start_time"] = "2026-10-04T01:00:59.96+10:00"
    else:
        reader.rows[b][0]["final"] = False
    with pytest.raises(Conflict):
        activity_analysis(reader, request([a, b]))


def test_zero_length_completed_record_and_date_boundaries():
    reader = MemoryReader()
    a = reader.add(1, "2026-10-04T00:00:00+10:00", [])
    b = reader.add(2, "2026-10-05T00:00:00+11:00", [span(0, 1500)])
    result = activity_analysis(reader, request([a, b]))
    assert result["calendar"]["daily"][0]["observed_s"] == 0
    assert len(result["sources"]) == 2


@pytest.mark.parametrize("changes", [
    {"timezone": "not/a/timezone"},
    {"date_from": "2026-10-05", "date_to": "2026-10-04"},
    {"date_from": "2026-10-01", "date_to": "2026-11-01"},
    {"run_ids": [str(UUID(int=n+1)) for n in range(101)]},
])
def test_request_bounds(changes):
    body = request().model_dump()
    with pytest.raises(ValidationError):
        ActivityAnalysisRequest(**(body | changes))


@pytest.mark.postgres
def test_postgres_and_http_reader_same_contract_and_authenticated_route(store, backend, run_request):
    ids = []
    for i in range(2):
        payload = run_request | {
            "request_key": uuid4().hex, "recording_id": f"analysis-{i}",
            "start_time": f"2026-10-04T01:{i:02d}:00+10:00",
        }
        run_id, _ = store.create(payload)
        x = np.zeros((500, 3), np.float32)
        valid = np.ones(500, bool)
        valid[100:125] = False
        store.accept(run_id, "batch", InputBatch.create(x, valid=valid, angular_velocity=np.zeros((len(x), 3), np.float32)))
        store.seal(run_id, 500)
        finish(Worker(store.settings, store, backend), store, run_id)
        ids.append(run_id)
    body = request(ids)
    expected = activity_analysis(PostgresReader(store), body)
    headers = {"Authorization": "Bearer "+store.settings.api_token}
    with TestClient(create_app(store.settings, store)) as client:
        assert client.post("/v2/analysis/activity", json=body.model_dump(mode="json")).status_code == 401
        response = client.post("/v2/analysis/activity", json=body.model_dump(mode="json"), headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == expected
        client.app.state.analysis_slots.acquire()
        try:
            busy = client.post("/v2/analysis/activity", json=body.model_dump(mode="json"), headers=headers)
            assert busy.status_code == 429 and busy.headers["Retry-After"] == "5"
            busy_calendar = client.get(f"/v2/runs/{ids[0]}/calendar", headers=headers, params={
                "timezone": "Australia/Sydney", "date_from": "2026-10-04", "date_to": "2026-10-04"})
            assert busy_calendar.status_code == 429
            assert client.get(f"/v2/runs/{ids[0]}", headers=headers).status_code == 200
        finally:
            client.app.state.analysis_slots.release()

        def forward(req):
            result = client.request(req.method, str(req.url), headers=dict(req.headers))
            return httpx.Response(result.status_code, json=result.json())

        http_reader = HttpResultReader("http://testserver", store.settings.api_token,
                                       transport=httpx.MockTransport(forward))
        try:
            assert activity_analysis(http_reader, body) == expected
            with pytest.raises(NotFound):
                http_reader.run(uuid4())
        finally:
            http_reader.close()
        conflict = store.create(run_request | {"request_key": uuid4().hex})[0]
        response = client.post("/v2/analysis/activity", headers=headers,
                               json=request([conflict]).model_dump(mode="json"))
        assert response.status_code == 409
        response = client.post("/v2/analysis/activity", headers=headers,
                               json=request([uuid4()]).model_dump(mode="json"))
        assert response.status_code == 404
