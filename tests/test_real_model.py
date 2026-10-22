import numpy as np
import pytest
from fastapi.testclient import TestClient
from imu_inference import InputBatch, InferenceEngine
from imu_inference.contracts import canonical_json
from imu_services.query import create_app
from imu_services.worker import Worker
from test_integration import finish

pytestmark = [pytest.mark.postgres, pytest.mark.model]


def test_real_model_checkpoint_resume_and_calendar(store, run_request):
    t = np.arange(3001)/25
    x = np.column_stack((.3*np.sin(t*2*np.pi*1.8), .15*np.cos(t*2*np.pi*1.8),
                         9.80665+1.7*np.sin(t*2*np.pi*1.8))).astype(np.float32)
    valid = np.ones(len(x), bool)
    valid[700:715] = False
    run_id, _ = store.create(run_request)
    engine = InferenceEngine(threads=2)
    store.accept(run_id, "first", InputBatch.create(x[:2250], valid=valid[:2250], angular_velocity=np.zeros((len(x[:2250]), 3), np.float32)))
    original = Worker(store.settings, store, engine.backend)
    original.once()
    original.once()
    with store.pool.connection() as c:
        snapshot = store._run(c, run_id)
        assert snapshot["processed_samples"] == 2250 and snapshot["summary"]["through_sample"] == 1500
    store.accept(run_id, "tail", InputBatch.create(x[2250:], start=2250, valid=valid[2250:], angular_velocity=np.zeros((len(x[2250:]), 3), np.float32)))
    store.seal(run_id, len(x))
    # New worker has no stream object. It must restore the committed JSON checkpoint.
    completed = finish(Worker(store.settings, store, engine.backend), store, run_id)
    expected = engine.predict(x, valid=valid, angular_velocity=np.zeros((len(x), 3), np.float32))
    assert completed["summary"] == expected["statistics"]
    with store.pool.connection() as c:
        rows = c.execute("SELECT events FROM results WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
    got = sorted(canonical_json(event) for row in rows for event in row["events"])
    assert got == sorted(map(canonical_json, expected["events"]))
    headers = {"Authorization": "Bearer "+store.settings.api_token}
    with TestClient(create_app(store.settings, store)) as query:
        calendar = query.get(f"/v2/runs/{run_id}/calendar", headers=headers,
                             params=dict(timezone="UTC", date_from="2026-10-20", date_to="2026-10-20"))
        assert calendar.status_code == 200, calendar.text
        day = calendar.json()["daily"][0]
        assert day["observed_s"] == completed["summary"]["observed_s"]
        assert day["invalid_s"] == completed["summary"]["invalid_s"]
        for label, seconds in completed["summary"]["duration_s"].items():
            assert day["duration_s"][label] == seconds
