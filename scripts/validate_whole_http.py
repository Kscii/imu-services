"""Actual HTTP admission at the 25-hour limit, with two concurrent gzip uploads.

No inference worker is started; this validates bounded API/DB admission only.
"""

import gzip
import json
import secrets
import subprocess
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import numpy as np
from container_acceptance import free_port


def main():
    api_port, query_port = free_port(), free_port()
    env = {
        k: secrets.token_hex(32)
        for k in (
            "IMU_ADMIN_DB_PASSWORD",
            "IMU_RUNTIME_DB_PASSWORD",
            "IMU_QUERY_DB_PASSWORD",
            "IMU_WRITE_TOKEN",
            "IMU_READ_TOKEN",
        )
    }
    env.update(
        IMU_RUNTIME_IMAGE="localhost/imu-runtime:1.0.0",
        IMU_QUERY_IMAGE="localhost/imu-query:1.0.0",
        IMU_API_PORT=str(api_port),
        IMU_QUERY_PORT=str(query_port),
        IMU_WORKERS="0",
        IMU_WHOLE_WORKERS="0",
    )
    headers = {
        "Authorization": "Bearer " + env["IMU_WRITE_TOKEN"],
        "Content-Type": "application/x-ndjson",
        "Content-Encoding": "gzip",
    }
    release = threading.Event()
    ready = [threading.Event(), threading.Event()]
    row = np.random.default_rng(25).normal(0, 0.1, (15000, 6))
    row[:, 1] += 9.80665
    block = "".join(
        json.dumps([*np.round(r, 4).tolist(), True, False], separators=(",", ":")) + "\n" for r in row
    ).encode()

    def header(key, n):
        return dict(
            format="imu.recording.ndjson.v2",
            request_key=key,
            recording_id=key,
            device_id="synthetic",
            start_time="2026-10-21T00:00:00Z",
            sample_count=n,
            steps_enabled=False,
        )

    def data(index):
        encoder = zlib.compressobj(1, zlib.DEFLATED, 31)
        yield encoder.compress(
            (json.dumps(header(f"concurrent-{index}", 2250000)) + "\n").encode()
        ) + encoder.flush(zlib.Z_SYNC_FLUSH)
        ready[index].set()
        if not release.wait(30):
            raise TimeoutError("test upload gate timed out")
        for _ in range(150):
            encoded = encoder.compress(block)
            if encoded:
                yield encoded
        yield encoder.flush()

    with tempfile.TemporaryDirectory(prefix="imu-r3-http-") as td:
        envfile = Path(td) / "test.env"
        envfile.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
        envfile.chmod(0o600)
        command = [
            "docker",
            "compose",
            "--env-file",
            str(envfile),
            "-p",
            "imu-r3-upload-acceptance",
            "-f",
            str(Path("compose.yaml").resolve()),
            "-f",
            str(Path("compose.offline.yaml").resolve()),
        ]

        def compose(*args):
            subprocess.run(command + list(args), check=True)

        started = time.monotonic()
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "180", "api", "query")

            def send(i):
                with httpx.Client(timeout=360) as client:
                    r = client.post(
                        f"http://127.0.0.1:{api_port}/v2/runs/whole-record", headers=headers, content=data(i)
                    )
                    r.raise_for_status()
                    return r.json()["run_id"]

            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(send, i) for i in range(2)]
                for event in ready:
                    assert event.wait(15)
                time.sleep(1)
                small = (json.dumps(header("overload", 1)) + "\n[0,9.8,0,0,0,0,true,false]\n").encode()
                response = httpx.post(
                    f"http://127.0.0.1:{api_port}/v2/runs/whole-record",
                    headers=headers,
                    content=gzip.compress(small),
                    timeout=10,
                )
                assert response.status_code == 429, response.text
                upload_start = time.monotonic()
                release.set()
                ids = [future.result(timeout=360) for future in futures]
                accepted = time.monotonic()
            with httpx.Client(
                base_url=f"http://127.0.0.1:{query_port}",
                headers={"Authorization": "Bearer " + env["IMU_READ_TOKEN"]},
            ) as query:
                runs = [query.get(f"/v2/runs/{rid}").json() for rid in ids]
                assert all(
                    r["status"] == "queued" and r["available_end"] == 2250000 and not r["input_cleared"]
                    for r in runs
                )
                assert all(query.get(f"/v2/runs/{rid}/results").json()["items"] == [] for rid in ids)
                modes = query.get("/v2/operations").json()["modes"]
                assert next(r for r in modes if r["mode"] == "whole_record")["queued"] == 2
            peak = int(
                subprocess.check_output(
                    ["docker", "exec", "imu-r3-upload-acceptance-api-1", "cat", "/sys/fs/cgroup/memory.peak"],
                    text=True,
                )
            )
            assert peak < 512 * 1024**2
            report = dict(
                schema="imu.whole.http-limit.v1",
                passed=True,
                record_samples=2250000,
                concurrent_uploads=2,
                third_upload_status=429,
                gzip=True,
                api_container_memory_limit_bytes=512 * 1024**2,
                api_container_peak_bytes=peak,
                upload_and_accept_seconds=accepted - upload_start,
                total_seconds=accepted - started,
                both_durably_queued=True,
                no_partial_results=True,
                model_compute_test=False,
            )
            output = Path("reports/whole-http-r3.json")
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report), flush=True)
        finally:
            release.set()
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
