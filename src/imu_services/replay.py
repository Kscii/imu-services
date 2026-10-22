"""Reusable HTTP replay client. Input files and detailed outputs stay on the test host."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import time
from uuid import uuid4

import httpx
import numpy as np

from imu_inference import InputBatch


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    """Owner-only atomic files; never include service credentials in reports."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+f".{uuid4().hex}.partial")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def load_records(manifest):
    """Read an explicit local manifest; no directory discovery or remote downloads."""
    manifest = Path(manifest)
    records = json.loads(manifest.read_text())
    if not isinstance(records, list) or not records:
        raise ValueError("manifest must be a nonempty list")
    output = []
    for index, row in enumerate(records):
        path = Path(row["path"])
        if not path.is_absolute():
            path = manifest.parent/path
        if len(row["sha256"]) != 64 or type(row["samples"]) is not int or row["samples"] <= 0:
            raise ValueError("every record needs a SHA256 and positive sample count")
        # Do not put participant metadata or original names into exported reports.
        output.append({"path": str(path.resolve()), "sha256": row["sha256"],
                       "samples": row["samples"], "cohort": row.get("cohort", "unspecified"),
                       "alias": f"record-{index:03d}"})
    return output


def load_input(record):
    path = Path(record["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("input file hash differs from the manifest")
    with np.load(path, allow_pickle=False) as z:
        if "acceleration_mps2" in z:
            x = z["acceleration_mps2"].copy()
            gyro = z["angular_velocity_rads"].copy() if "angular_velocity_rads" in z else None
        else:
            values = z["x"]
            if values.ndim != 2 or values.shape[1] != 6:
                raise ValueError("v2 input requires six columns, including observed gyroscope values")
            x = values[:, :3].copy()
            gyro = values[:, 3:].copy()
        valid = z["valid"].copy() if "valid" in z else np.ones(len(x), bool)
        excluded = z["excluded"].copy() if "excluded" in z else np.zeros(len(x), bool)
    batch = InputBatch.create(x, valid=valid, excluded=excluded, angular_velocity=gyro)
    if batch.end != record["samples"]:
        raise ValueError("sample count differs from the manifest")
    return batch


def canonical_events(events):
    # Publication grouping and activity/step interleaving are transport details.
    return sorted(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events)


def compare_result(actual, expected):
    return {"statistics_equal": actual["statistics"] == expected["statistics"],
            "events_equal": canonical_events(actual["events"]) == canonical_events(expected["events"]),
            "provenance_equal": actual["provenance"] == expected["provenance"]}


def chunk_ranges(n, seconds, *, irregular=False, seed=0):
    if seconds <= 0 or seconds*25 != int(seconds*25) or seconds > 600:
        raise ValueError("chunk length must be a positive whole sample and at most 600 seconds")
    rng, result, start = random.Random(seed), [], 0
    while start < n:
        count = rng.choice([25, 125, 1500, 7500]) if irregular else int(seconds*25)
        end = min(n, start+count)
        result.append((start, end))
        start = end
    return result


class PlannedInterruption(Exception):
    """The caller must resume with the same state file and unchanged request."""


@dataclass
class ReplayTiming:
    request_s: list
    retries: dict

    def summary(self):
        return {"requests": len(self.request_s),
                "request_p50_s": float(np.percentile(self.request_s, 50)) if self.request_s else None,
                "request_p95_s": float(np.percentile(self.request_s, 95)) if self.request_s else None,
                "request_max_s": max(self.request_s, default=None), "retries": self.retries}


class ReplayClient:
    def __init__(self, api_url, query_url, write_token, read_token, *, transport=None, sleep=time.sleep):
        self.api_url, self.query_url = api_url.rstrip("/"), query_url.rstrip("/")
        self.api = httpx.Client(base_url=self.api_url, timeout=30, transport=transport,
                                headers={"Authorization": "Bearer "+write_token})
        self.query = httpx.Client(base_url=self.query_url, timeout=30, transport=transport,
                                  headers={"Authorization": "Bearer "+read_token})
        self.sleep = sleep
        self.timing = ReplayTiming([], {})
        self.drop_acknowledgement = False

    @classmethod
    def from_env(cls):
        return cls(*(os.environ[k] for k in
                     ("IMU_API_URL", "IMU_QUERY_URL", "IMU_WRITE_TOKEN", "IMU_READ_TOKEN")))

    def close(self):
        self.api.close()
        self.query.close()

    def request(self, method, path, *, body=None, query=False):
        client = self.query if query else self.api
        for attempt in range(12):
            start = time.monotonic()
            reason = None
            response = None
            try:
                response = client.request(method, path, json=body) if body is not None else client.request(method, path)
                if self.drop_acknowledgement and method == "POST" and response.is_success:
                    self.drop_acknowledgement = False
                    reason = "simulated_lost_acknowledgement"
                elif response.status_code in (429, 502, 503, 504):
                    reason = str(response.status_code)
                else:
                    response.raise_for_status()
                    return response.json()
            except httpx.TransportError:
                reason = "transport_error"
            finally:
                self.timing.request_s.append(time.monotonic()-start)
            self.timing.retries[reason] = self.timing.retries.get(reason, 0)+1
            delay = min(30, .25*2**attempt)
            if response is not None:
                try:
                    delay = max(delay, min(60, float(response.headers.get("Retry-After", 0))))
                except ValueError:
                    pass
            self.sleep(delay+random.uniform(0, .05))
        raise TimeoutError("retry budget exhausted; retain state and retry unchanged")

    def run(self, run_id):
        return self.request("GET", f"/v2/runs/{run_id}", query=True)

    def result(self, run_id):
        run = self.run(run_id)
        if run["status"] != "completed":
            raise ValueError("final result requested before completion")
        events, cursor, finals = [], 0, 0
        while True:
            page = self.request("GET", f"/v2/runs/{run_id}/results?after={cursor}&limit=500", query=True)
            if not page["items"]:
                break
            for row in page["items"]:
                if row["id"] <= cursor:
                    raise ValueError("nonadvancing result cursor")
                cursor = row["id"]
                events.extend(row["events"])
                finals += bool(row["final"])
        if finals != 1:
            raise ValueError("expected exactly one final result")
        return {"statistics": run["summary"], "provenance": run["provenance"], "events": events}

    def wait(self, run_id, *, timeout=900, on_poll=None):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            run = self.run(run_id)
            if on_poll:
                on_poll(run)
            if run["status"] == "completed":
                return run
            if run["status"] == "failed":
                raise RuntimeError("inference run failed; inspect the source-host logs")
            self.sleep(.5)
        raise TimeoutError("run did not complete before the test deadline")

    def submit(self, batch, *, identity, state_path, steps_enabled=True, chunk_seconds=30,
               irregular=False, reorder=False, duplicate=False, gap=None, interrupt_after=None,
               speed=0, start_time="2026-10-21T00:00:00Z", on_upload=None, after_upload=None,
               on_start=None):
        ranges = chunk_ranges(batch.end, chunk_seconds, irregular=irregular)
        if gap:
            if not 0 <= gap[0] < gap[1] <= batch.end:
                raise ValueError("gap must be inside the recording")
            # Split on exact gap boundaries, so missing input is sent through /gaps.
            edges = sorted({a for r in ranges for a in r} | set(gap))
            ranges = list(zip(edges, edges[1:]))
        if reorder:
            ranges = ranges[1::2]+ranges[::2]
        if speed < 0 or (speed and reorder):
            raise ValueError("paced replay requires ordered chunks and a nonnegative speed")
        config = {
            "api_url": self.api_url, "query_url": self.query_url, "identity": identity,
            "input_digest": batch.digest, "steps_enabled": steps_enabled, "ranges": ranges, "gap": gap,
            "start_time": start_time,
        }
        key = digest_json(config)
        state_path = Path(state_path)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["config_sha256"] != key:
                raise ValueError("resume configuration changed; choose a new state file")
        else:
            state = {"config_sha256": key, "run_id": None, "receipts": {}, "sealed": False}
        if state["run_id"] is None:
            created = self.request("POST", "/v2/runs", body={
                "request_key": identity, "recording_id": identity, "device_id": identity.split(":")[0],
                "start_time": start_time, "steps_enabled": steps_enabled,
            })
            state["run_id"] = created["run_id"]
            write_json(state_path, state)
        run_id = state["run_id"]
        started, count = time.monotonic(), 0
        if on_start:
            on_start(run_id, time.time())
        for a, b in ranges:
            batch_id = f"part-{a}-{b}"
            if batch_id in state["receipts"]:
                continue
            if speed:
                self.sleep(max(0, started+b/25/speed-time.monotonic()))
            if on_upload:
                on_upload(run_id, a, b, time.time())
            if gap and a >= gap[0] and b <= gap[1]:
                path = f"/v2/runs/{run_id}/gaps"
                payload = dict(batch_id=batch_id, start_sample=a, sample_count=b-a)
            else:
                part = batch.slice(a, b)
                path = f"/v2/runs/{run_id}/batches"
                payload = dict(batch_id=batch_id, start_sample=a, sample_rate_hz=25,
                               acceleration_mps2=part.acceleration.tolist(),
                               valid=part.valid.tolist(), excluded=part.excluded.tolist())
                if part.angular_velocity is not None:
                    payload["angular_velocity_rads"] = part.angular_velocity.tolist()
            receipt = self.request("POST", path, body=payload)
            accepted_at = time.time()
            if duplicate:
                repeated = self.request("POST", path, body=payload)
                if not repeated["duplicate"]:
                    raise ValueError("duplicate retry did not return the original receipt")
            state["receipts"][batch_id] = {"start": a, "end": b, "accepted_at": accepted_at,
                                           "available_end": receipt["available_end"]}
            write_json(state_path, state)
            if after_upload:
                after_upload(run_id, a, b, accepted_at)
            count += 1
            if interrupt_after is not None and count == interrupt_after:
                raise PlannedInterruption()
        if not state["sealed"]:
            self.request("POST", f"/v2/runs/{run_id}/seal", body={"end_sample": batch.end})
            state["sealed"] = True
            state["sealed_at"] = datetime.now(timezone.utc).isoformat()
            write_json(state_path, state)
        return run_id
