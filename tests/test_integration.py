from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4
import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient
from imu_inference import InputBatch, InferenceEngine
from imu_services.api import create_app
from imu_services.query import create_app as query_app
from imu_services.store import Conflict, LostLease, Overloaded
from imu_services.worker import Worker

pytestmark = pytest.mark.postgres


def finish(worker, store, run_id):
    for _ in range(50):
        worker.once()
        with store.pool.connection() as c:
            run = store._run(c, run_id)
        if run["status"] in ("completed", "failed"):
            assert run["status"] == "completed", run["last_error"]
            return run
    raise AssertionError("run did not complete")


def test_http_lifecycle_duplicate_ordering_and_immutable_results(store, backend, run_request):
    headers = {"Authorization": "Bearer "+store.settings.api_token}
    n = 3301
    x = np.column_stack((np.arange(n), np.zeros(n), np.full(n, 9.8))).astype(np.float32)
    with TestClient(create_app(store.settings, store)) as api, TestClient(query_app(store.settings, store)) as query:
        assert api.post("/v2/runs", json=run_request).status_code == 401
        response = api.post("/v2/runs", json=run_request, headers=headers)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert api.post("/v2/runs", json=run_request, headers=headers).json()["duplicate"]
        assert api.post("/v2/runs", json={**run_request, "device_id": "different"},
                        headers=headers).status_code == 409
        batches = []
        for a, b in [(2250, n), (0, 1000), (1000, 2250)]:
            body = dict(batch_id=f"b-{a}", start_sample=a, acceleration_mps2=x[a:b].tolist())
            body["angular_velocity_rads"] = np.zeros((b-a, 3)).tolist()
            batches.append(body)
            got = api.post(f"/v2/runs/{run_id}/batches", json=body, headers=headers)
            assert got.status_code == 202, got.text
        assert api.post(f"/v2/runs/{run_id}/seal", json={"end_sample": n}, headers=headers).status_code == 202
        run = finish(Worker(store.settings, store, backend), store, run_id)
        expected = InferenceEngine(backend=backend).predict(x, angular_velocity=np.zeros((len(x), 3), np.float32))
        assert run["summary"] == expected["statistics"]
        assert api.post(f"/v2/runs/{run_id}/batches", json=batches[0], headers=headers).json()["duplicate"]
        batches[0]["acceleration_mps2"][0][0] += 1
        assert api.post(f"/v2/runs/{run_id}/batches", json=batches[0], headers=headers).status_code == 409
        rows = query.get(f"/v2/runs/{run_id}/results", headers=headers).json()["items"]
        assert rows and rows[-1]["final"]
        assert rows[-1]["provenance"]["library_version"] == "1.0.1"
        last = rows[-1]["id"]
        assert query.get(f"/v2/runs/{run_id}/results?after={last}", headers=headers).json()["items"] == []
        assert "checkpoint" not in query.get(f"/v2/runs/{run_id}", headers=headers).json()
        assert query.get(f"/v2/runs/{run_id}/analysis", headers=headers).json()["summary"] == run["summary"]
        with store.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM input_batches WHERE payload IS NOT NULL").fetchone()["n"] == 0
            assert c.execute("SELECT pending_bytes FROM admission_budget").fetchone()["pending_bytes"] == 0
        with pytest.raises(psycopg.errors.RaiseException):
            with store.pool.connection() as c:
                c.execute("UPDATE results SET events='[]' WHERE id=%s", (last,))


def test_explicit_gap_seal_rate_and_body_limits(store, backend, run_request):
    headers = {"Authorization": "Bearer "+store.settings.api_token}
    with TestClient(create_app(store.settings, store)) as api:
        run_id = api.post("/v2/runs", json=run_request, headers=headers).json()["run_id"]
        body = dict(batch_id="later", start_sample=100, acceleration_mps2=[[0, 0, 9.8]]*250)
        body["angular_velocity_rads"] = [[0, 0, 0]]*250
        assert api.post(f"/v2/runs/{run_id}/batches", json={**body, "sample_rate_hz": 50},
                        headers=headers).status_code == 422
        assert api.post(f"/v2/runs/{run_id}/batches", json=body, headers=headers).status_code == 202
        assert api.post(f"/v2/runs/{run_id}/seal", json={"end_sample": 350}, headers=headers).status_code == 409
        assert api.post(f"/v2/runs/{run_id}/gaps",
                        json={"batch_id": "gap", "start_sample": 0, "sample_count": 100},
                        headers=headers).status_code == 202
        assert api.post(f"/v2/runs/{run_id}/seal", json={"end_sample": 350}, headers=headers).status_code == 202
        run = finish(Worker(store.settings, store, backend), store, run_id)
        assert run["summary"]["invalid_s"] == 4
        assert run["summary"]["observed_s"] == 10
    tiny = replace(store.settings, max_body_bytes=64)
    with TestClient(create_app(tiny, store)) as api:
        assert api.post("/v2/runs", content=b"x"*65, headers=headers).status_code == 413


def test_fencing_recovery_and_concurrent_claims(store, backend, run_request):
    run_id, _ = store.create(run_request)
    store.accept(run_id, "one", InputBatch.create([[0, 0, 9.8]]*250, angular_velocity=np.zeros((len([[0, 0, 9.8]]*250), 3), np.float32)))
    store.seal(run_id, 250)
    engine = InferenceEngine(backend=backend)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: store.claim([engine.fingerprint]), range(2)))
    assert sum(c is not None for c in claims) == 1
    old = next(c for c in claims if c)
    with store.pool.connection() as c:
        c.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    new = store.claim([engine.fingerprint])
    assert new["lease_token"] > old["lease_token"]
    with pytest.raises(LostLease):
        store.commit(run_id, old["lease_token"], 250, {}, [], {}, True)
    assert store.heartbeat(run_id, old["lease_token"]) is False
    assert store.heartbeat(run_id, new["lease_token"]) is True
    with store.pool.connection() as c:
        c.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    finish(Worker(store.settings, store, backend), store, run_id)
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM results WHERE run_id=%s", (run_id,)).fetchone()["n"] == 1


def test_commit_rollback_keeps_input_and_checkpoint(store, backend, run_request):
    run_id, _ = store.create(run_request)
    store.accept(run_id, "one", InputBatch.create([[0, 0, 9.8]]*250, angular_velocity=np.zeros((len([[0, 0, 9.8]]*250), 3), np.float32)))
    store.seal(run_id, 250)
    with store.pool.connection() as c:
        c.execute("""CREATE FUNCTION fail_insert() RETURNS trigger LANGUAGE plpgsql AS $$
                     BEGIN RAISE EXCEPTION 'injected commit failure'; END $$""")
        c.execute("CREATE TRIGGER fail_result BEFORE INSERT ON results FOR EACH ROW EXECUTE FUNCTION fail_insert()")
    worker = Worker(store.settings, store, backend)
    worker.once()
    with store.pool.connection() as c:
        row = store._run(c, run_id)
        assert row["processed_samples"] == 0 and row["checkpoint"] is None
        assert c.execute("SELECT payload IS NOT NULL AS kept FROM input_batches").fetchone()["kept"]
        assert c.execute("SELECT count(*) AS n FROM results").fetchone()["n"] == 0
        c.execute("DROP TRIGGER fail_result ON results")
        c.execute("UPDATE runs SET next_attempt=now() WHERE id=%s", (run_id,))
    finish(worker, store, run_id)


def test_admission_bounds_conflicts_and_result_revisions(store, backend, run_request):
    run_id, _ = store.create(run_request)
    original = store.settings
    store.settings = replace(original, max_pending_samples_per_run=200)
    with pytest.raises(Overloaded):
        store.accept(run_id, "large", InputBatch.create([[0, 0, 9.8]]*201, angular_velocity=np.zeros((len([[0, 0, 9.8]]*201), 3), np.float32)))
    store.settings = original
    store.accept(run_id, "first", InputBatch.create([[0, 0, 9.8]]*100, angular_velocity=np.zeros((len([[0, 0, 9.8]]*100), 3), np.float32)))
    with pytest.raises(Conflict):
        store.accept(run_id, "overlap", InputBatch.create([[0, 0, 9.8]]*100, start=50, angular_velocity=np.zeros((len([[0, 0, 9.8]]*100), 3), np.float32)))
    store.seal(run_id, 100)
    worker = Worker(store.settings, store, backend)
    finish(worker, store, run_id)
    other, _ = store.create({**run_request, "request_key": uuid4().hex, "steps_enabled": False})
    store.seal(other, 0)
    finish(worker, store, other)
    with store.pool.connection() as c:
        rows = c.execute("SELECT id,provenance FROM query_v1_runs WHERE recording_id='recording-1'").fetchall()
        assert len(rows) == 2 and rows[0]["provenance"] != rows[1]["provenance"]


def test_create_retry_survives_changed_deployment_defaults(store, run_request):
    for requested in (None, True):
        request = {**run_request, "request_key": uuid4().hex, "steps_enabled": requested}
        store.settings = replace(store.settings, steps_enabled=True)
        original, _ = store.create(request)
        store.settings = replace(store.settings, steps_enabled=False)
        repeated, duplicate = store.create(request)
        assert repeated == original and duplicate
    with pytest.raises(Conflict, match="GP2 disabled"):
        store.create({**run_request, "request_key": uuid4().hex, "steps_enabled": True})
