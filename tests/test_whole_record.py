import gzip
import json
from dataclasses import replace
from uuid import uuid4
import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient
from imu_inference import InferenceEngine, PipelineConfig
from imu_services.api import create_app
from imu_services.codec import canonical_rows
from imu_services.ingest import RecordUpload, UploadTooLarge
from imu_services.settings import Settings
from imu_services.store import Conflict, LostLease, Overloaded
from imu_services.worker import Worker
from imu_services.readers import PostgresReader


def recording(key=None, n=1601):
    h = dict(
        format="imu.recording.ndjson.v2",
        mode="whole_record",
        request_key=key or uuid4().hex,
        recording_id="whole-test",
        device_id="synthetic",
        start_time="2026-10-21T00:00:00Z",
        sample_rate_hz=25,
        sample_count=n,
        steps_enabled=False,
    )
    return (json.dumps(h) + "\n" + ("[0,9.8,0,0,0,0,true,false]\n" * n)).encode()


def upload(settings, body=None):
    u = RecordUpload(settings)
    u.feed(body or recording())
    return u.finish()


@pytest.mark.parametrize("compressed", [False, True])
def test_stream_parser_fragmentation_and_canonical_hash(compressed):
    import hashlib

    body = recording(n=25)
    data = gzip.compress(body) if compressed else body
    u = RecordUpload(Settings("", ""), "gzip" if compressed else "identity")
    try:
        for i in range(0, len(data), 7):
            u.feed(data[i : i + 7])
        u.finish()
        assert u.count == 25
        assert (
            u.digest.hexdigest()
            == hashlib.sha256(b"".join(canonical_rows(b) for b in u.batches())).hexdigest()
        )
    finally:
        u.close()


@pytest.mark.parametrize("data", [recording(n=1)[:-2], gzip.compress(recording(n=1))[:-8]])
def test_incomplete_stream_is_rejected(data):
    u = RecordUpload(Settings("", ""), "gzip" if data[:2] == b"\x1f\x8b" else "identity")
    try:
        with pytest.raises(ValueError):
            u.feed(data)
            u.finish()
    finally:
        u.close()


def test_decompression_bomb_is_bounded():
    u = RecordUpload(replace(Settings("", ""), max_whole_decoded_bytes=1000), "gzip")
    try:
        with pytest.raises(UploadTooLarge):
            u.feed(gzip.compress(recording()))
    finally:
        u.close()


@pytest.mark.postgres
def test_whole_http_atomic_success_idempotency_and_pool_isolation(store, backend, run_request):
    body = recording()
    headers = {"Authorization": "Bearer " + store.settings.api_token, "Content-Type": "application/x-ndjson"}
    with TestClient(create_app(store.settings, store)) as api:
        assert api.post("/v2/runs/whole-record", content=body).status_code == 401
        response = api.post(
            "/v2/runs/whole-record",
            content=gzip.compress(body),
            headers={**headers, "Content-Encoding": "gzip"},
        )
        assert response.status_code == 202, response.text
        rid = response.json()["run_id"]
        reader = PostgresReader(store)
        assert reader.results(rid) == []
        incremental, _ = store.create(run_request)
        assert not Worker(store.settings, store, backend).once()
        whole = Worker(replace(store.settings, worker_mode="whole_record"), store, backend)
        assert whole.once()
        run = reader.run(rid)
        assert run["status"] == "completed" and run["input_cleared"]
        assert run["mode"] == "whole_record" and run["diagnostics"]
        rows = reader.results(rid)
        expected = InferenceEngine(
            PipelineConfig(mode="whole_record", steps_enabled=False), backend=backend
        ).predict(np.tile([0, 9.8, 0], (1601, 1)), angular_velocity=np.zeros((1601, 3)))
        assert [e for r in rows for e in r["events"]] == expected["events"]
        assert run["summary"] == expected["statistics"]
        assert rows[-1]["final"] and reader.run(incremental)["status"] == "waiting"
        assert api.post("/v2/runs/whole-record", content=body, headers=headers).json()["duplicate"]
        changed = body.replace(b"[0,9.8", b"[1,9.8", 1)
        assert api.post("/v2/runs/whole-record", content=changed, headers=headers).status_code == 409
        with store.pool.connection() as c:
            assert c.execute("SELECT sum(pending_bytes) AS n FROM admission_budget").fetchone()["n"] == 0
            assert not c.execute("SELECT 1 FROM input_batches WHERE payload IS NOT NULL").fetchone()


@pytest.mark.postgres
def test_whole_rollback_retry_and_terminal_cleanup(store, backend):
    u = upload(store.settings)
    try:
        rid, _ = store.create_whole(u)
        engine = InferenceEngine(PipelineConfig(mode="whole_record", steps_enabled=False), backend=backend)
        run = store.claim([engine.fingerprint], "whole_record")
        # Simulate a lost worker and ensure stale publication is rejected.
        with store.pool.connection() as c:
            c.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,))
        with pytest.raises(LostLease):
            store.commit_whole(rid, run["lease_token"], {})
        new = store.claim([engine.fingerprint], "whole_record")
        assert new["lease_token"] > run["lease_token"]
        # A failed result INSERT must leave no pages visible and retain all input.
        with store.pool.connection() as c:
            c.execute(
                "CREATE FUNCTION fail_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
            )
            c.execute(
                "CREATE TRIGGER fail_result BEFORE INSERT ON results FOR EACH ROW EXECUTE FUNCTION fail_insert()"
            )
        result = engine.predict(np.tile([0, 9.8, 0], (1601, 1)), angular_velocity=np.zeros((1601, 3)))
        with pytest.raises(psycopg.errors.RaiseException):
            store.commit_whole(rid, new["lease_token"], result)
        assert PostgresReader(store).results(rid) == []
        store.fail(rid, new["lease_token"], "TEST_FAILURE")
        with store.pool.connection() as c:
            assert (
                c.execute("SELECT count(*) AS n FROM input_batches WHERE payload IS NOT NULL").fetchone()["n"]
                > 0
            )
            c.execute(
                "UPDATE runs SET failures=%s,next_attempt=now() WHERE id=%s",
                (store.settings.max_failures - 1, rid),
            )
        run = store.claim([engine.fingerprint], "whole_record")
        store.fail(rid, run["lease_token"], "FINAL_TEST_FAILURE")
        current = PostgresReader(store).run(rid)
        assert current["status"] == "failed" and current["input_cleared"] and current["input_sha256"]
        assert store.create_whole(u) == (rid, True)
        with pytest.raises(Conflict, match="new request key"):
            store.retry(rid)
        with store.pool.connection() as c:
            assert c.execute("SELECT sum(pending_bytes) AS n FROM admission_budget").fetchone()["n"] == 0
            assert not c.execute("SELECT 1 FROM input_batches WHERE payload IS NOT NULL").fetchone()
    finally:
        u.close()


@pytest.mark.postgres
def test_rejected_upload_has_no_durable_partial_task(store):
    settings = replace(store.settings, max_whole_pending_bytes=1)
    old = store.settings
    store.settings = settings
    u = upload(settings)
    try:
        with pytest.raises(Overloaded):
            store.create_whole(u)
        with store.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0
            assert c.execute("SELECT sum(pending_bytes) AS n FROM admission_budget").fetchone()["n"] == 0
    finally:
        store.settings = old
        u.close()


@pytest.mark.postgres
def test_configuration_pinning_and_expired_terminal_lease(store, backend):
    from imu_services.release import check_upgrade

    u = upload(store.settings)
    try:
        store.settings = replace(store.settings, sign_adaptation=True)
        rid, _ = store.create_whole(u)
        with pytest.raises(RuntimeError, match="unfinished runs"):
            check_upgrade(store.settings.database_url)
        assert check_upgrade(store.settings.database_url, sign_adaptation=True)["compatible"]
        engine = InferenceEngine(
            PipelineConfig(mode="whole_record", sign_adaptation=True, steps_enabled=False), backend=backend
        )
        run = store.claim([engine.fingerprint], "whole_record")
        with store.pool.connection() as c:
            c.execute(
                "UPDATE runs SET failures=%s,lease_until=now()-interval '1 second' WHERE id=%s",
                (store.settings.max_failures - 1, rid),
            )
        assert store.claim([engine.fingerprint], "whole_record") is None
        row = PostgresReader(store).run(rid)
        assert row["status"] == "failed" and row["input_cleared"]
        assert row["config"]["sign_adaptation"]
        assert check_upgrade(store.settings.database_url)["compatible"]
        with pytest.raises(LostLease):
            store.fail(rid, run["lease_token"], "late")
    finally:
        u.close()


@pytest.mark.postgres
def test_cross_mode_same_key_conflict_and_maintenance(store, run_request):
    rid, _ = store.create(run_request)
    u = upload(store.settings, recording(key=run_request["request_key"]))
    try:
        with pytest.raises(Conflict):
            store.create_whole(u)
        store.settings = replace(store.settings, admit_new_runs=False)
        assert store.create(run_request) == (rid, True)
        with pytest.raises(Overloaded):
            store.create({**run_request, "request_key": uuid4().hex})
    finally:
        u.close()


@pytest.mark.postgres
def test_upload_timeout_waits_for_parser_before_cleanup(store, monkeypatch):
    import time

    finished = []
    original = RecordUpload.feed

    def slow_feed(self, data):
        time.sleep(0.04)
        assert not self.spool.closed
        original(self, data)
        finished.append(True)

    monkeypatch.setattr(RecordUpload, "feed", slow_feed)
    settings = replace(store.settings, upload_timeout_seconds=0.01)
    app = create_app(settings, store)
    headers = {"Authorization": "Bearer " + settings.api_token, "Content-Type": "application/x-ndjson"}
    with TestClient(app) as client:
        r = client.post("/v2/runs/whole-record", content=recording(n=1), headers=headers)
        assert r.status_code == 408 and finished and app.state.upload_slots == 0
        with store.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0
