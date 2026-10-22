"""Short synthetic synthetic HTTP replay using only the Python standard library.

Uses the public HTTP contract; never reads the task database or prints credentials.
The accelerated fixture demonstrates service behavior, not recognition accuracy.
"""

import argparse
import gzip
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--api-url")
    parser.add_argument("--query-url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/smoke/result.json"))
    args = parser.parse_args()
    settings = {}
    for line in args.env_file.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    for key in ("IMU_WRITE_TOKEN", "IMU_READ_TOKEN"):
        if not settings.get(key):
            raise ValueError(f"Missing {key}; use the private smoke environment file")
    api_url = args.api_url or "http://127.0.0.1:" + settings.get("IMU_API_PORT", "8100")
    query_url = args.query_url or "http://127.0.0.1:" + settings.get("IMU_QUERY_PORT", "8101")

    def request(path, body=None, *, query=False, compressed=False):
        headers = {"Authorization": "Bearer " + settings["IMU_READ_TOKEN" if query else "IMU_WRITE_TOKEN"]}
        data = None
        if body is not None:
            data = body if compressed else json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/x-ndjson" if compressed else "application/json"
            if compressed:
                headers["Content-Encoding"] = "gzip"
        req = urllib.request.Request((query_url if query else api_url).rstrip("/") + path,
                                     data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    started = time.monotonic()
    prefix = "synthetic-smoke-" + uuid4().hex
    n = 3750  # 150 seconds; fabricated axes and timestamps, no participant signal.
    signal = [[0.3 * math.sin(i / 25 * 2 * math.pi * 1.8),
               0.15 * math.cos(i / 25 * 2 * math.pi * 1.8),
               9.80665 + 1.7 * math.sin(i / 25 * 2 * math.pi * 1.8)] for i in range(n)]
    gyro = [[0.0, 0.0, 0.0] for _ in range(n)]  # Synthetic only, never substitute for observed gyro.
    valid = [not 800 <= i < 825 for i in range(n)]
    identity = dict(recording_id=prefix, device_id="synthetic-smoke-device",
                    start_time="2026-10-21T00:00:00Z", steps_enabled=True)
    run = request("/v2/runs", {**identity, "request_key": prefix + "-incremental"})
    incremental_id = run["run_id"]
    print(json.dumps(dict(stage="created", mode="incremental", run_id=incremental_id,
                          synthetic=True, accelerated_replay=True)), flush=True)
    for start in range(0, n, 750):
        end = min(n, start + 750)
        request(f"/v2/runs/{incremental_id}/batches", dict(
            batch_id=f"batch-{start}", start_sample=start, sample_rate_hz=25,
            acceleration_mps2=signal[start:end], angular_velocity_rads=gyro[start:end],
            valid=valid[start:end]))
    request(f"/v2/runs/{incremental_id}/seal", dict(end_sample=n))
    header = dict(**identity, request_key=prefix + "-whole", format="imu.recording.ndjson.v2",
                  mode="whole_record", sample_rate_hz=25, sample_count=n)
    lines = [json.dumps(header)] + [json.dumps([*a, *g, v, False])
                                   for a, g, v in zip(signal, gyro, valid, strict=True)]
    whole = request("/v2/runs/whole-record", gzip.compress(("\n".join(lines) + "\n").encode()),
                    compressed=True)
    ids = {"incremental": incremental_id, "whole_record": whole["run_id"]}
    previous, completed = {}, {}
    deadline = time.monotonic() + 300
    while len(completed) < len(ids):
        if time.monotonic() > deadline:
            raise TimeoutError("Smoke timed out; inspect task status before retrying the test")
        for mode, run_id in ids.items():
            if mode in completed:
                continue
            run = request(f"/v2/runs/{run_id}", query=True)
            state = (run["status"], run["summary"].get("through_sample", 0) if run["summary"] else 0)
            if previous.get(mode) != state:
                print(json.dumps(dict(stage="progress", mode=mode, run_id=run_id,
                                      status=state[0], classified_samples=state[1])), flush=True)
                previous[mode] = state
            if run["status"] == "failed":
                raise RuntimeError(f"Smoke task failed: {run_id}; inspect its status through the query API")
            if run["status"] == "completed":
                if run["summary"]["through_sample"] != n or not run["summary"]["finished"]:
                    raise RuntimeError("Completed task has an incomplete summary")
                page = request(f"/v2/runs/{run_id}/results?limit=500", query=True)
                if sum(bool(row["final"]) for row in page["items"]) != 1:
                    raise RuntimeError("Short smoke expected exactly one final result")
                completed[mode] = dict(run_id=run_id, provenance=run["provenance"],
                                       statistics=run["summary"], result_pages=len(page["items"]),
                                       analysis=request(f"/v2/runs/{run_id}/analysis", query=True))
                print(json.dumps(dict(stage="result", mode=mode, synthetic=True,
                                      activity_seconds=run["summary"]["duration_s"],
                                      observed_seconds=run["summary"]["observed_s"],
                                      estimated_steps=run["summary"]["steps"]["estimated_step_count"],
                                      steps_experimental=True), indent=2), flush=True)
        if len(completed) < len(ids):
            time.sleep(0.5)
    for mode, run in completed.items():
        calendar = request(f"/v2/runs/{run['run_id']}/calendar?timezone=UTC&date_from=2026-10-21&date_to=2026-10-21", query=True)
        analysis = request("/v2/analysis/activity", dict(participant_id="synthetic-smoke",
            run_ids=[run["run_id"]], timezone="UTC", date_from="2026-10-21", date_to="2026-10-21"), query=True)
        assert calendar["steps"] == analysis["calendar"]["steps"]
        assert calendar["steps"]["estimated_step_count"] == run["statistics"]["steps"]["estimated_step_count"]
        assert calendar["daily"][0]["observed_s"] == 149
        run["calendar_steps"] = calendar["steps"]
        run["analysis_version"] = analysis["analysis_version"]
    result = dict(schema="imu.synthetic.smoke.v1", passed=True, synthetic=True,
                  accelerated_replay=True, accuracy_evidence=False, samples=n,
                  seconds=time.monotonic() - started, results=completed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(dict(stage="completed", report=str(args.output), synthetic=True,
                          accuracy_evidence=False, seconds=result["seconds"])), flush=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Smoke HTTP error {exc.code}; check credentials, admission and task state") from None
