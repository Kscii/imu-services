"""Real HTTP + PostgreSQL + CPU worker burst test; synthetic inputs, never accuracy evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4
import httpx
import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from imu_services.store import migrate
from imu_services.release import release_info


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def memory(pid):
    try:
        text = Path(f"/proc/{pid}/smaps_rollup").read_text()
        return {key: int(next(line.split()[1] for line in text.splitlines() if line.startswith(key+":")))
                for key in ("Rss", "Pss")}
    except (FileNotFoundError, StopIteration):
        return {"Rss": 0, "Pss": 0}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--devices", default="1,10,25,50,100")
    p.add_argument("--seconds", type=int, default=180)
    p.add_argument("--paced", action="store_true", help="Send each 30s batch after 30s of wall time")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    url = os.environ["IMU_TEST_DATABASE_URL"]
    schema = "bench_"+uuid4().hex
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(url, options=f"-csearch_path={schema}")
    migrate(dsn)
    token = uuid4().hex+uuid4().hex
    api_port, query_port = port(), port()
    env = {**os.environ, "IMU_DATABASE_URL": dsn, "IMU_WRITE_TOKEN": token, "IMU_READ_TOKEN": token,
           "IMU_THREADS": str(a.threads), "OMP_NUM_THREADS": str(a.threads),
           "OPENBLAS_NUM_THREADS": str(a.threads), "MKL_NUM_THREADS": str(a.threads),
           "IMU_POOL_SIZE": "4"}
    report = {"schema": "imu.capacity.burst.v1", "input": "deterministic synthetic 25Hz acceleration",
              "accuracy_evidence": False, "sustained_load_test": False,
              "paced_short_duration_test": a.paced,
              "host": platform.platform(), "logical_cpus": os.cpu_count(), "workers": a.workers,
              "threads_per_worker": a.threads, "seconds_per_device": a.seconds,
              "gp2_enabled": True, "scenarios": []}
    report["release"] = release_info()
    report["started_utc"] = datetime.now(timezone.utc).isoformat()
    report["memory_sampling"] = "Application process PSS/RSS sampled after all inputs admitted; excludes database"
    procs, logfiles = [], []
    temp = tempfile.TemporaryDirectory(prefix="imu-capacity-")
    try:
        for name, command in [("api", ["api", "--host", "127.0.0.1", "--port", str(api_port)]),
                              ("query", ["query", "--host", "127.0.0.1", "--port", str(query_port)])]+[
                                  (f"worker{i}", ["worker"]) for i in range(a.workers)]:
            log = open(Path(temp.name)/f"{name}.log", "w")
            logfiles.append(log)
            procs.append(subprocess.Popen([sys.executable, "-m", "imu_services.cli", *command],
                                          env=env, stdout=log, stderr=log))
        api = httpx.Client(base_url=f"http://127.0.0.1:{api_port}",
                           headers={"Authorization": "Bearer "+token}, timeout=60)
        query = httpx.Client(base_url=f"http://127.0.0.1:{query_port}",
                             headers={"Authorization": "Bearer "+token}, timeout=60)
        started = time.monotonic()
        while True:
            if any(proc.poll() is not None for proc in procs):
                raise RuntimeError("benchmark child exited")
            try:
                ready = api.get("/health/ready").status_code == 200 and query.get("/health/ready").status_code == 200
                with psycopg.connect(dsn) as c:
                    workers = c.execute("SELECT count(*) FROM query_v1_workers").fetchone()[0]
                if ready and workers == a.workers:
                    break
            except httpx.ConnectError:
                pass
            if time.monotonic()-started > 180:
                raise TimeoutError("workers did not initialize")
            time.sleep(.5)
        report["startup_seconds"] = time.monotonic()-started
        samples = a.seconds*25
        t = np.arange(samples)/25
        x = np.column_stack((.3*np.sin(t*2*np.pi*1.8), .15*np.cos(t*2*np.pi*1.8),
                             9.80665+1.7*np.sin(t*2*np.pi*1.8))).astype(np.float32)
        x[(t % 100) > 75] = [0, 0, 9.80665]
        with psycopg.connect(dsn, autocommit=True) as c:
            for devices in map(int, a.devices.split(",")):
                latencies, retry_codes = [], []
                started = time.monotonic()
                def post(path, body):
                    for attempt in range(8):
                        before = time.monotonic()
                        response = api.post(path, json=body)
                        latencies.append(time.monotonic()-before)
                        if response.status_code not in (429, 503):
                            response.raise_for_status()
                            return response.json()
                        retry_codes.append(response.status_code)
                        time.sleep(min(5, .2*2**attempt))
                    raise RuntimeError("admission retries exhausted")

                def send(device):
                    identity = f"synthetic-{devices}-{device}"
                    run_id = post("/v2/runs", dict(request_key=identity, recording_id=identity,
                                  device_id=identity, start_time="2026-10-20T00:00:00Z"))["run_id"]
                    for start in range(0, samples, 750):
                        if a.paced:
                            deadline = started+min(samples, start+750)/25
                            time.sleep(max(0, deadline-time.monotonic()))
                        post(f"/v2/runs/{run_id}/batches",
                             dict(batch_id=f"b{start}", start_sample=start,
                                  acceleration_mps2=x[start:start+750].tolist(),
                                  angular_velocity_rads=np.zeros((len(x[start:start+750]),3)).tolist()))
                    post(f"/v2/runs/{run_id}/seal", dict(end_sample=samples))
                    return run_id

                with ThreadPoolExecutor(devices if a.paced else min(devices, 16)) as pool:
                    ids = list(pool.map(send, range(devices)))
                admitted = time.monotonic()-started
                peak_pss = peak_rss = peak_pending = 0
                query_times = []
                while True:
                    counts = dict(c.execute("SELECT status,count(*) FROM runs WHERE id=ANY(%s::uuid[]) "
                                            "GROUP BY status", (ids,)).fetchall())
                    if counts.get("failed"):
                        raise RuntimeError(f"failed runs: {counts}")
                    pending = c.execute("SELECT pending_bytes FROM admission_budget").fetchone()[0]
                    peak_pending = max(peak_pending, pending)
                    rss = [memory(proc.pid) for proc in procs]
                    peak_rss = max(peak_rss, sum(v["Rss"] for v in rss))
                    peak_pss = max(peak_pss, sum(v["Pss"] for v in rss))
                    before = time.monotonic()
                    query.get(f"/v2/runs/{ids[0]}").raise_for_status()
                    query_times.append(time.monotonic()-before)
                    if counts.get("completed") == devices:
                        break
                    if time.monotonic()-started > 3600:
                        raise TimeoutError("queue did not drain within 1 hour")
                    if any(proc.poll() is not None for proc in procs):
                        raise RuntimeError("benchmark child exited during load")
                    time.sleep(.5)
                elapsed = time.monotonic()-started
                summaries = c.execute("SELECT summary FROM runs WHERE id=ANY(%s::uuid[])", (ids,)).fetchall()
                assert all(row[0] == summaries[0][0] for row in summaries), "cross-worker output mismatch"
                assert c.execute("SELECT count(*) FROM input_batches WHERE run_id=ANY(%s::uuid[]) "
                                 "AND payload IS NOT NULL", (ids,)).fetchone()[0] == 0
                item = {"devices": devices, "admission_seconds": admitted, "drain_total_seconds": elapsed,
                        "after_final_upload_seconds": elapsed-admitted,
                        "equivalent_device_rate": devices*a.seconds/elapsed,
                        "admission_p50_ms": float(np.percentile(latencies, 50)*1000),
                        "admission_p95_ms": float(np.percentile(latencies, 95)*1000),
                        "admission_p99_ms": float(np.percentile(latencies, 99)*1000),
                        "read_p95_ms": float(np.percentile(query_times, 95)*1000),
                        "retry_responses": len(retry_codes), "peak_process_pss_mib": peak_pss/1024,
                        "peak_process_rss_sum_mib": peak_rss/1024, "peak_pending_payload_bytes": peak_pending,
                        "identical_final_statistics_across_devices": True, "inputs_cleared": True}
                item["estimated_steps_per_record"] = summaries[0][0]["steps"]["estimated_step_count"]
                report["scenarios"].append(item)
                a.output.parent.mkdir(parents=True, exist_ok=True)
                a.output.write_text(json.dumps(report, indent=2)+"\n")
                print(json.dumps(item), flush=True)
        api.close()
        query.close()
    finally:
        for proc in procs:
            proc.send_signal(signal.SIGTERM) if proc.poll() is None else None
        for proc in procs:
            try:
                proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logfiles:
            log.close()
        if sys.exc_info()[0]:
            for path in Path(temp.name).glob("*.log"):
                print(path.name, path.read_text()[-4000:], file=sys.stderr)
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        temp.cleanup()


if __name__ == "__main__":
    main()
