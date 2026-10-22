"""Submit one already standardized recording. Never silently fabricate gyroscope data."""

import argparse
import gzip
import json
import os
from pathlib import Path
import httpx
import numpy as np
from imu_inference import InputBatch
from imu_inference.contracts import MAX_RECORD_SAMPLES


def write_recording(destination, header, batch):
    if batch.start != 0 or batch.end > MAX_RECORD_SAMPLES:
        raise ValueError("complete recordings must start at zero and span at most 25 hours")
    header = {
        **header,
        "format": "imu.recording.ndjson.v2",
        "mode": "whole_record",
        "sample_rate_hz": 25,
        "sample_count": batch.end,
    }
    with gzip.open(destination, "wt", encoding="utf-8") as f:
        f.write(json.dumps(header, allow_nan=False) + "\n")
        for a, g, v, e in zip(
            batch.acceleration, batch.angular_velocity, batch.valid, batch.excluded, strict=True
        ):
            f.write(json.dumps([*a.tolist(), *g.tolist(), bool(v), bool(e)], allow_nan=False) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("input", type=Path, help="NPZ with acceleration_mps2 and angular_velocity_rads")
    p.add_argument(
        "--header",
        required=True,
        type=Path,
        help="JSON: stable request_key, recording_id, device_id, start_time",
    )
    p.add_argument(
        "--output", required=True, type=Path, help="temporary .ndjson.gz; keep until durable acceptance"
    )
    args = p.parse_args()
    with np.load(args.input, allow_pickle=False) as z:
        batch = InputBatch.create(
            z["acceleration_mps2"],
            angular_velocity=z["angular_velocity_rads"],
            valid=z.get("valid"),
            excluded=z.get("excluded"),
        )
    write_recording(args.output, json.loads(args.header.read_text()), batch)
    # Retry the exact same file and request_key after a timeout/lost acknowledgment.
    with args.output.open("rb") as f, httpx.Client(timeout=360) as client:
        r = client.post(
            os.environ["IMU_API_URL"].rstrip("/") + "/v2/runs/whole-record",
            content=f,
            headers={
                "Authorization": "Bearer " + os.environ["IMU_WRITE_TOKEN"],
                "Content-Type": "application/x-ndjson",
                "Content-Encoding": "gzip",
            },
        )
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
