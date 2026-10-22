"""Read-only analysis acceptance using completed real-data replay runs on Ubuntu."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import httpx

from imu_services.analysis import activity_analysis
from imu_services.models import ActivityAnalysisRequest
from imu_services.readers import HttpResultReader
from imu_services.replay import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/kscii/imu-validation-r2"))
    args = parser.parse_args()
    root = args.root.resolve()
    if root != Path("/home/kscii/imu-validation-r2"):
        parser.error("this test reads only the dedicated R2 environment")
    started = datetime.now(timezone.utc)
    matrix = json.loads((root/"matrix/summary.json").read_text())
    if matrix["status"] != "PASS" or matrix["cases"] != 290:
        raise ValueError("complete the real replay matrix first")
    index_path = root/"references/index.json"
    if hashlib.sha256(index_path.read_bytes()).hexdigest() != matrix["reference_index_sha256"]:
        raise ValueError("reference identity changed after the matrix")
    index = json.loads(index_path.read_text())
    cases = {row["number"]: row for row in json.loads((root/"matrix/cases.json").read_text())}
    env = dict(line.split("=", 1) for line in (root/"test.env").read_text().splitlines()
               if line and not line.startswith("#"))
    base = "http://127.0.0.1:"+env["IMU_QUERY_PORT"]
    reader = HttpResultReader(base, env["IMU_READ_TOKEN"])
    reports, selected_ids = [], []
    try:
        with httpx.Client(base_url=base, headers={"Authorization": "Bearer "+env["IMU_READ_TOKEN"]},
                          timeout=60) as client:
            for enabled, offset in ((False, 1), (True, 4)):
                run_ids, expected, runs = [], {}, []
                for i, record in enumerate(index["records"]):
                    number = i*6+offset  # matrix: GP2 off/on, each with 10/30/90-second packets
                    assert cases[number]["passed"]
                    assert cases[number]["options"] == {"chunk_seconds": 30, "steps_enabled": enabled}
                    state = json.loads((root/"matrix/states"/f"{number:05d}.json").read_text())
                    run_id = state["run_id"]
                    run_ids.append(run_id)
                    runs.append(reader.run(run_id))
                    reference = record["configs"]["on" if enabled else "off"]
                    path = root/"references"/reference["file"]
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
                    expected[run_id] = json.loads(path.read_text())["statistics"]
                zone = ZoneInfo("Australia/Sydney")
                starts = [datetime.fromisoformat(run["start_time"].replace("Z", "+00:00")) for run in runs]
                ends = [start+timedelta(microseconds=run["sealed_end"]*40000)
                        for start, run in zip(starts, runs)]
                body = {
                    "participant_id": "synthetic-merged-replay-fixture", "run_ids": run_ids,
                    "timezone": zone.key, "date_from": min(starts).astimezone(zone).date().isoformat(),
                    "date_to": max(ends).astimezone(zone).date().isoformat(),
                }
                request_started = time.monotonic()
                response = client.post("/v2/analysis/activity", json=body)
                elapsed = time.monotonic()-request_started
                response.raise_for_status()
                result = response.json()
                assert len(result["sources"]) == len(run_ids) == 47
                assert result["subject_assignment"] == "caller_asserted"
                for scale in ("hourly", "daily", "weekly"):
                    buckets = result["calendar"][scale]
                    for metric in ("observed_s", "invalid_s", "excluded_s"):
                        assert math.isclose(sum(b[metric] for b in buckets),
                                            sum(s[metric] for s in expected.values()), abs_tol=1e-6)
                    for label in next(iter(expected.values()))["duration_s"]:
                        assert math.isclose(sum(b["duration_s"][label] or 0 for b in buckets),
                                            sum(s["duration_s"][label] for s in expected.values()), abs_tol=1e-6)
                    for bucket in buckets:
                        assert math.isclose(sum(bucket[k] for k in
                                                ("observed_s", "invalid_s", "excluded_s", "unobserved_s")),
                                            bucket["calendar_s"], abs_tol=1e-6)
                for row in result["recording_steps"]:
                    assert row["scope"] == "whole_recording_not_date_clipped"
                    assert row["statistics"] == expected[row["run_id"]]["steps"]
                # A separately composed HTTP reader must reproduce the actual endpoint result.
                assert activity_analysis(reader, ActivityAnalysisRequest(**body)) == result
                duplicate = client.post("/v2/analysis/activity",
                                        json=body | {"run_ids": list(reversed(run_ids))+[run_ids[0]]})
                duplicate.raise_for_status()
                repeated = duplicate.json()
                assert repeated.pop("duplicate_run_ids_removed") == 1
                assert result.pop("duplicate_run_ids_removed") == 0
                assert repeated == result
                selected_ids.append(run_ids[0])
                reports.append({
                    "steps_enabled": enabled, "records": len(run_ids),
                    "samples": sum(s["through_sample"] for s in expected.values()),
                    "endpoint_wall_s": elapsed, "response_bytes": len(response.content),
                    "hour_day_week_totals_match_direct_reference": True,
                    "coverage_accounting_equal": True, "whole_record_step_statistics_equal": True,
                    "http_reader_equal": True, "duplicate_and_reordered_selection_equal": True,
                })
            mixed = client.post("/v2/analysis/activity", json=body | {"run_ids": selected_ids})
            assert mixed.status_code == 409
    finally:
        reader.close()
    write_json(root/"analysis-acceptance.json", {
        "status": "PASS", "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(), "configurations": reports,
        "mixed_processing_fingerprints_rejected": True,
        "reference_index_sha256": matrix["reference_index_sha256"],
        "scope": "47 existing real-data runs per configuration; synthetic combined subject and replay dates",
        "concurrent_workload": "small analysis acceptance probe while ten-client soak runs",
        "model_accuracy_claim": False, "data_movement": "individual reports and predictions remain on Ubuntu",
    })
    print("Real replay analysis passed: 47 records x 2 configurations; hourly/daily/weekly accounting equal.")


if __name__ == "__main__":
    main()
