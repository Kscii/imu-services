import hashlib
import json
from uuid import uuid4

import httpx
import numpy as np
import pytest

from imu_inference import InputBatch
from imu_services.replay import (
    PlannedInterruption, ReplayClient, chunk_ranges, compare_result, load_input, load_records,
)


def test_manifest_hash_and_six_axis_input_adapter(tmp_path):
    path = tmp_path/"signal.npz"
    x = np.arange(600, dtype=np.float32).reshape(100, 6)
    valid = np.ones(100, bool)
    valid[30:40] = False
    np.savez(path, x=x, valid=valid, y=np.zeros(100, np.int16))
    manifest = tmp_path/"manifest.json"
    manifest.write_text(json.dumps([{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "samples": 100, "metadata": {"participant": "not-exported"}}]))
    record = load_records(manifest)[0]
    assert "metadata" not in record
    batch = load_input(record)
    np.testing.assert_array_equal(batch.acceleration, x[:, :3])
    np.testing.assert_array_equal(batch.angular_velocity, x[:, 3:])
    np.testing.assert_array_equal(batch.valid, valid)
    assert not batch.excluded.any()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        load_input(record)


def test_ranges_and_comparison_preserve_every_sample():
    for irregular in (False, True):
        ranges = chunk_ranges(7011, 30, irregular=irregular, seed=17)
        assert ranges[0][0] == 0 and ranges[-1][1] == 7011
        assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
        assert all(0 < b-a <= 15000 for a, b in ranges)
    expected = dict(statistics={"n": 2}, provenance={"fixed": True}, events=[{"x": 1}, {"x": 2}])
    assert all(compare_result(expected | {"events": list(reversed(expected["events"]))}, expected).values())
    assert not compare_result(expected | {"events": expected["events"]*2}, expected)["events_equal"]


class Receiver:
    def __init__(self):
        self.run_id = str(uuid4())
        self.receipts, self.calls = {}, []
        self.overload_once = True
        self.sealed = False

    def handle(self, request):
        payload = json.loads(request.content)
        self.calls.append(request.url.path)
        if self.overload_once:
            self.overload_once = False
            return httpx.Response(503, headers={"Retry-After": "0"}, json={"error": "busy"})
        if request.url.path == "/v2/runs":
            return httpx.Response(202, json={"run_id": self.run_id, "duplicate": True})
        if request.url.path.endswith("/seal"):
            self.sealed = True
            return httpx.Response(202, json={"sealed_end": payload["end_sample"]})
        batch_id = payload["batch_id"]
        duplicate = batch_id in self.receipts
        if duplicate:
            assert self.receipts[batch_id] == payload
        self.receipts[batch_id] = payload
        end = payload["start_sample"]+payload.get("sample_count", len(payload.get("acceleration_mps2", [])))
        return httpx.Response(202, json={"duplicate": duplicate, "available_end": end})


def client(receiver):
    return ReplayClient("http://api", "http://query", "write-secret", "read-secret",
                        transport=httpx.MockTransport(receiver.handle), sleep=lambda seconds: None)


def test_lost_acknowledgement_duplicate_resume_and_state_identity(tmp_path):
    receiver = Receiver()
    batch = InputBatch.create(np.zeros((1250, 3), np.float32), angular_velocity=np.zeros((len(np.zeros((1250, 3), np.float32)), 3), np.float32))
    state = tmp_path/"state.json"
    first = client(receiver)
    first.drop_acknowledgement = True
    with pytest.raises(PlannedInterruption):
        first.submit(batch, identity="test", state_path=state, chunk_seconds=10,
                     duplicate=True, interrupt_after=1)
    assert first.timing.retries == {"503": 1, "simulated_lost_acknowledgement": 1}
    first.close()
    assert not receiver.sealed
    second = client(receiver)
    assert second.submit(batch, identity="test", state_path=state, chunk_seconds=10,
                         duplicate=True) == receiver.run_id
    assert receiver.sealed and len(receiver.receipts) == 5
    saved = state.read_text()
    assert "secret" not in saved and "Authorization" not in saved
    assert state.stat().st_mode & 0o777 == 0o600
    calls = len(receiver.calls)
    second.submit(batch, identity="test", state_path=state, chunk_seconds=10, duplicate=True)
    assert len(receiver.calls) == calls
    with pytest.raises(ValueError, match="configuration changed"):
        second.submit(batch, identity="changed", state_path=state, chunk_seconds=10)
    second.close()


def test_explicit_gap_uses_gap_endpoint_and_reordered_uploads(tmp_path):
    receiver = Receiver()
    c = client(receiver)
    c.submit(InputBatch.create(np.zeros((2500, 3), np.float32), angular_velocity=np.zeros((len(np.zeros((2500, 3), np.float32)), 3), np.float32)), identity="gap",
             state_path=tmp_path/"state.json", chunk_seconds=30, gap=(1000, 1250), reorder=True)
    assert any(path.endswith("/gaps") for path in receiver.calls)
    gap = next(payload for payload in receiver.receipts.values() if "sample_count" in payload)
    assert gap["start_sample"] == 1000 and gap["sample_count"] == 250
    c.close()


def test_nonretryable_client_error_is_not_retried(tmp_path):
    attempts = []

    def reject(request):
        attempts.append(1)
        return httpx.Response(409, json={"error": "CONFLICT"})

    c = ReplayClient("http://api", "http://query", "write", "read",
                     transport=httpx.MockTransport(reject), sleep=lambda seconds: None)
    with pytest.raises(httpx.HTTPStatusError):
        c.submit(InputBatch.create([[0, 0, 9.8]], angular_velocity=np.zeros((len([[0, 0, 9.8]]), 3), np.float32)), identity="bad", state_path=tmp_path/"state.json")
    assert len(attempts) == 1
    c.close()
