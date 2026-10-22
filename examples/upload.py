"""Submit standardized six-axis input; credentials are environment variables."""
import argparse
import os
import random
import time
import httpx
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("npz", help="NPZ with acceleration_mps2 and angular_velocity_rads, optional masks")
    parser.add_argument("--recording-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--request-key", required=True, help="Keep this unchanged when retrying the same run")
    parser.add_argument("--start-time", required=True, help="ISO timestamp with explicit UTC offset")
    args = parser.parse_args()
    with np.load(args.npz, allow_pickle=False) as data:
        x = data["acceleration_mps2"].copy()
        gyro = data["angular_velocity_rads"].copy()
        masks = {k: data[k].copy() for k in ("valid", "excluded") if k in data}
    if x.ndim != 2 or x.shape[1] != 3 or not np.isfinite(x).all():
        raise ValueError("finite N x 3 acceleration required")
    if gyro.shape != x.shape or not np.isfinite(gyro).all():
        raise ValueError("matching finite angular velocity required")
    with httpx.Client(base_url=os.environ["IMU_API_URL"], timeout=30,
                      headers={"Authorization": "Bearer "+os.environ["IMU_WRITE_TOKEN"]}) as client:
        def post(path, payload):
            for attempt in range(8):
                try:
                    response = client.post(path, json=payload)
                    if response.status_code not in (429, 503):
                        response.raise_for_status()
                        return response.json()
                    delay = max(float(response.headers.get("Retry-After", 5)), min(30, 2**attempt))
                except httpx.TransportError:
                    delay = min(30, 2**attempt)
                time.sleep(delay+random.uniform(0, .5))
            raise RuntimeError("retry budget exhausted; retain data and retry with the same identities")

        run = post("/v2/runs", dict(request_key=args.request_key, recording_id=args.recording_id,
                                   device_id=args.device_id, start_time=args.start_time))
        run_id = run["run_id"]
        for start in range(0, len(x), 750):
            end = min(len(x), start+750)
            post(f"/v2/runs/{run_id}/batches",
                 dict(batch_id=f"chunk-{start}", start_sample=start, acceleration_mps2=x[start:end].tolist(),
                      angular_velocity_rads=gyro[start:end].tolist(),
                      **{key: value[start:end].tolist() for key, value in masks.items()}))
        post(f"/v2/runs/{run_id}/seal", {"end_sample": len(x)})
        print(run_id)


if __name__ == "__main__":
    main()
