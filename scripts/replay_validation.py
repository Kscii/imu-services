"""Source-host HTTP acceptance and paced load. Detailed evidence is not exported."""
import argparse
import fcntl
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

import numpy as np

from imu_services.replay import (
    PlannedInterruption, ReplayClient, compare_result, load_input, load_records, write_json,
)


def distribution(values):
    return {"n": len(values), "p50": float(np.percentile(values, 50)) if values else None,
            "p95": float(np.percentile(values, 95)) if values else None, "max": max(values, default=None)}


def parse_bytes(value):
    if isinstance(value, (int, float)):
        return value
    text = value.split("/")[0].strip()
    for unit, scale in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10),
                        ("GB", 10**9), ("MB", 10**6), ("kB", 1000), ("B", 1)):
        if text.endswith(unit):
            return float(text[:-len(unit)])*scale
    raise ValueError("unrecognized Docker memory unit")


class Monitor:
    """Observe first visibility independently of upload pacing; poll resolution is explicit."""
    def __init__(self, output, compose):
        self.output, self.compose = output, compose
        self.client = ReplayClient.from_env()
        self.active, self.lock, self.stop = {}, threading.Lock(), threading.Event()
        self.resources_ready = threading.Event()
        self.lags, self.errors, self.resources = defaultdict(list), [], []
        self.query_latencies = []
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.resource_thread = threading.Thread(target=self.sample_resources, daemon=True)
        self.thread.start()
        self.resource_thread.start()
        if not self.resources_ready.wait(45) or self.errors:
            self.stop.set()
            raise RuntimeError("initial resource sample failed; see the source-host test environment")

    def register(self, run_id, started, samples, speed):
        with self.lock:
            self.active.setdefault(run_id, {"origin": started, "samples": samples, "speed": speed,
                                            "cursor": 0, "receipts": [], "done": False, "first_activity": False})

    def accepted(self, run_id, a, b, timestamp):
        with self.lock:
            self.active[run_id]["receipts"].append((a, b, timestamp))

    def poll(self):
        while not self.stop.is_set():
            with self.lock:
                pending = [(key, dict(value)) for key, value in self.active.items() if not value["done"]]
            for run_id, item in pending:
                try:
                    started = time.monotonic()
                    page = self.client.request(
                        "GET", f"/v2/runs/{run_id}/results?after={item['cursor']}&limit=500", query=True)
                    self.query_latencies.append(time.monotonic()-started)
                    visible = time.time()
                    for row in page["items"]:
                        if row["id"] <= item["cursor"]:
                            raise ValueError("nonadvancing monitor cursor")
                        item["cursor"] = row["id"]
                        published = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
                        self.lags["publication_to_first_observed_s"].append(max(0, visible-published))
                        for event in row["events"]:
                            kind = event["kind"]
                            if kind not in ("activity_core", "step_segment"):
                                continue
                            end = event["end"]
                            if item["speed"]:
                                ready = item["origin"]+end/25/item["speed"]
                                self.lags[f"{kind}_end_to_visible_s"].append(max(0, visible-ready))
                            receipts = [t for a, b, t in item["receipts"] if a < end <= b]
                            if receipts:
                                self.lags[f"{kind}_end_accepted_to_visible_s"].append(max(0, visible-min(receipts)))
                            if kind == "activity_core" and not item["first_activity"]:
                                self.lags["first_activity_since_record_start_s"].append(visible-item["origin"])
                                item["first_activity"] = True
                        if row["final"]:
                            item["done"] = True
                    with self.lock:
                        for key in ("cursor", "done", "first_activity"):
                            self.active[run_id][key] = item[key]
                except Exception as exc:
                    self.errors.append(type(exc).__name__)
            self.stop.wait(1)

    def sample_resources(self):
        sql = """SELECT json_build_object(
            'database_bytes',pg_database_size(current_database()),
            'wal_directory_bytes',(SELECT coalesce(sum(size),0) FROM pg_ls_waldir()),
            'public_table_bytes',(SELECT coalesce(sum(pg_total_relation_size(oid)),0)
              FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'),
            'pending_payload_bytes',(SELECT pending_bytes FROM admission_budget),
            'input_payloads_remaining',(SELECT count(*) FROM input_batches WHERE payload IS NOT NULL),
            'oldest_pending_age_s',(SELECT extract(epoch FROM now()-min(created_at))
              FROM input_batches WHERE payload IS NOT NULL),
            'statuses',(SELECT json_object_agg(status,n) FROM
              (SELECT status,count(*) AS n FROM runs GROUP BY status) s));"""
        while not self.stop.is_set():
            try:
                ids = subprocess.check_output(self.compose+["ps", "-q"], text=True, timeout=20).split()
                stats = subprocess.check_output(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                                                 text=True, timeout=20) if ids else ""
                containers = [json.loads(line) for line in stats.splitlines() if line]
                database = json.loads(subprocess.check_output(
                    self.compose+["exec", "-T", "postgres", "psql", "-U", "imu_admin", "-d", "imu",
                                  "-At", "-c", sql], text=True, timeout=20))
                row = {"utc": datetime.now(timezone.utc).isoformat(), "database": database,
                       "containers": containers,
                       "container_memory_usage_bytes": sum(parse_bytes(r["MemUsage"]) for r in containers)}
                self.resources.append(row)
                with (self.output/"resources.jsonl").open("a") as f:
                    f.write(json.dumps(row)+"\n")
                self.resources_ready.set()
            except Exception as exc:
                self.errors.append("resource:"+type(exc).__name__)
                self.resources_ready.set()
            self.stop.wait(15)

    def finish(self):
        # Allow final publications to be observed without keeping the caller waiting indefinitely.
        limit = time.monotonic()+10
        while time.monotonic() < limit:
            with self.lock:
                if all(item["done"] for item in self.active.values()):
                    break
            time.sleep(.2)
        self.stop.set()
        self.thread.join(35)
        self.resource_thread.join(45)
        self.client.close()
        with self.lock:
            unfinished = sum(not item["done"] for item in self.active.values())
        return {"poll_interval_s": 1, "resource_sample_interval_s": 15,
                "query_response_s": distribution(self.query_latencies),
                "latencies_s": {key: distribution(value) for key, value in self.lags.items()},
                "observation_errors": dict(Counter(self.errors)), "unobserved_final_results": unfinished,
                "resource_samples": len(self.resources),
                "peak_container_memory_usage_bytes": max(
                    (r["container_memory_usage_bytes"] for r in self.resources), default=None),
                "initial_database": self.resources[0]["database"] if self.resources else None,
                "last_sampled_database": self.resources[-1]["database"] if self.resources else None,
                "clock": "source and service on same host; replay acquisition clock is simulated",
                "memory_scope": "Docker reported container usage, including PostgreSQL; excludes host caches",
                "latency_scope": "sample-range end to first HTTP observation; not every sample's latency"}


def expected_for(references, item, enabled, gap):
    row = next(r for r in references["records"] if r["alias"] == item["alias"])
    if row["input_sha256"] != item["sha256"]:
        raise ValueError("reference input mismatch")
    ref = row["configs"][("on" if enabled else "off")+("-gap" if gap else "")]
    path = references["directory"]/ref["file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
        raise ValueError("reference result hash mismatch")
    return json.loads(path.read_text())


def case_run(item, options, *, args, references, monitor, session, number, restart=False):
    identity = f"r2-{session}-device{number % args.devices:02d}:{number:05d}:{item['alias']}"
    client = ReplayClient.from_env()
    batch, started = load_input(item), time.monotonic()
    state_path = args.output/"states"/f"{number:05d}.json"
    enabled = options.get("steps_enabled", True)
    gap = options.get("gap")
    try:
        def register(run_id, origin):
            monitor.register(run_id, origin, batch.end, options.get("speed", 0))

        def restart_after_checkpoint(run_id, a, b, timestamp):
            monitor.accepted(run_id, a, b, timestamp)
            if a:
                return
            deadline = time.monotonic()+300
            while time.monotonic() < deadline:
                if client.run(run_id)["processed_samples"] >= b:
                    break
                time.sleep(.5)
            else:
                raise TimeoutError("worker did not commit a checkpoint before restart")
            subprocess.run(args.compose+["kill", "-s", "SIGKILL", "worker"], check=True,
                           stdout=subprocess.DEVNULL, timeout=30)
            subprocess.run(args.compose+["up", "-d", "--no-deps", "worker"], check=True,
                           stdout=subprocess.DEVNULL, timeout=180)

        kwargs = dict(identity=identity, state_path=state_path, on_start=register,
                      start_time=(datetime(2026, 10, 21, tzinfo=timezone.utc)+timedelta(hours=number)).isoformat(),
                      after_upload=restart_after_checkpoint if restart else monitor.accepted, **options)
        if options.get("duplicate"):
            client.drop_acknowledgement = True
        interrupted = False
        if options.get("irregular"):
            try:
                client.submit(batch, interrupt_after=1, **kwargs)
            except PlannedInterruption:
                interrupted = True
                client.close()
                client = ReplayClient.from_env()
        run_id = client.submit(batch, **kwargs)
        client.wait(run_id)
        actual = client.result(run_id)
        expected = expected_for(references, item, enabled, gap)
        comparisons = compare_result(actual, expected)
        # Detailed result files contain movement information; keep them on the source host.
        write_json(args.output/"results"/f"{number:05d}.json", actual)
        return {"number": number, "cohort": item["cohort"], "samples": batch.end, "options": options,
                "passed": all(comparisons.values()), **comparisons, "resumed_client": interrupted,
                "worker_restarted": restart, "wall_s": time.monotonic()-started,
                "http": client.timing.summary()}
    finally:
        client.close()


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("smoke", "matrix", "soak", "recovery"))
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--references", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--compose-file", type=Path, action="append", required=True)
    p.add_argument("--compose-env", type=Path, required=True)
    p.add_argument("--project", default="imu-validation-r2")
    p.add_argument("--devices", type=int, default=10)
    p.add_argument("--seconds", type=int, default=7200)
    args = p.parse_args()
    if not args.project.startswith("imu-validation-r2"):
        p.error("only the dedicated imu-validation-r2 project may be controlled")
    if not 1 <= args.devices <= 100 or args.seconds <= 0:
        p.error("devices must be 1..100 and duration positive")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_lock = (args.output/".lock").open("w")
    fcntl.flock(session_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    args.compose = ["docker", "compose", "--env-file", str(args.compose_env.resolve()), "-p", args.project]
    for path in args.compose_file:
        args.compose.extend(["-f", str(path.resolve())])
    # Credentials belong only to the private test env file and HTTP clients.
    env = dict(line.split("=", 1) for line in args.compose_env.read_text().splitlines()
               if line and not line.startswith("#"))
    for key in ("IMU_WRITE_TOKEN", "IMU_READ_TOKEN"):
        os.environ[key] = env[key]
    os.environ["IMU_API_URL"] = f"http://127.0.0.1:{env['IMU_API_PORT']}"
    os.environ["IMU_QUERY_URL"] = f"http://127.0.0.1:{env['IMU_QUERY_PORT']}"
    records = load_records(args.manifest)
    references = json.loads((args.references/"index.json").read_text())
    if references["manifest_sha256"] != hashlib.sha256(args.manifest.read_bytes()).hexdigest():
        raise ValueError("reference manifest changed")
    references["directory"] = args.references
    session_path = args.output/"session.json"
    descriptor = {"mode": args.mode, "manifest_sha256": references["manifest_sha256"],
                  "reference_index_sha256": hashlib.sha256((args.references/"index.json").read_bytes()).hexdigest(),
                  "devices": args.devices, "seconds": args.seconds, "project": args.project}
    if session_path.exists():
        session_data = json.loads(session_path.read_text())
        if session_data["descriptor"] != descriptor or args.mode == "soak":
            raise ValueError("changed session configuration, or resumed soak: choose a new output directory")
    else:
        session_data = {"id": uuid4().hex[:16], "descriptor": descriptor}
        write_json(session_path, session_data)
    monitor = Monitor(args.output, args.compose)
    started, rows, failures = time.monotonic(), [], []
    lock = threading.Lock()

    def execute(record, options, number, restart=False):
        try:
            row = case_run(record, options, args=args, references=references, monitor=monitor,
                           session=session_data["id"], number=number, restart=restart)
        except Exception as exc:
            row = {"number": number, "cohort": record["cohort"], "passed": False,
                   "error_type": type(exc).__name__, "error": str(exc)}
        with lock:
            rows.append(row)
            if not row["passed"]:
                failures.append(row)
            write_json(args.output/"cases.json", rows)
            write_json(args.output/"progress.json", {"completed": len(rows), "failed": len(failures),
                                                     "elapsed_s": time.monotonic()-started})
        print(f"{args.mode}: completed={len(rows)} failed={len(failures)}", flush=True)
        return row

    monitor_summary = {}
    try:
        if args.mode == "soak":
            def device(index):
                cycle, offset = 0, 0
                while time.monotonic()-started < args.seconds:
                    record = records[(index+offset) % len(records)]
                    execute(record, {"steps_enabled": True, "chunk_seconds": 30, "speed": 1},
                            index+cycle*args.devices)
                    cycle, offset = cycle+1, offset+args.devices
            with ThreadPoolExecutor(args.devices) as pool:
                list(pool.map(device, range(args.devices)))
        elif args.mode == "recovery":
            record = max((r for r in records if r["cohort"] == "device"), key=lambda r: r["samples"])
            execute(record, {"steps_enabled": True, "chunk_seconds": 90}, 0, restart=True)
        else:
            cases = []
            selected = records[:1] if args.mode == "smoke" else records
            for record in selected:
                for enabled in (False, True):
                    for seconds in ((30,) if args.mode == "smoke" else (10, 30, 90)):
                        cases.append((record, {"steps_enabled": enabled, "chunk_seconds": seconds}))
            if args.mode == "matrix":
                for cohort in sorted({r["cohort"] for r in records}):
                    record = max((r for r in records if r["cohort"] == cohort), key=lambda r: r["samples"])
                    for enabled in (False, True):
                        cases.append((record, {"steps_enabled": enabled, "chunk_seconds": 30,
                                               "irregular": True, "reorder": True, "duplicate": True}))
                        cases.append((record, {"steps_enabled": enabled, "chunk_seconds": 30,
                                               "irregular": True, "gap": (1000, 1250)}))
            with ThreadPoolExecutor(min(4, len(cases))) as pool:
                futures = [pool.submit(execute, record, options, i) for i, (record, options) in enumerate(cases)]
                for future in as_completed(futures):
                    future.result()
    finally:
        monitor_summary = monitor.finish()
    # Read the drained database once more, so a periodic sample cannot hide the final state.
    cleanup_sql = """SELECT json_build_object(
      'pending_bytes',(SELECT pending_bytes FROM admission_budget),
      'payloads',(SELECT count(*) FROM input_batches WHERE payload IS NOT NULL),
      'unfinished',(SELECT count(*) FROM runs WHERE status!='completed'),
      'database_bytes',pg_database_size(current_database()),
      'wal_directory_bytes',(SELECT coalesce(sum(size),0) FROM pg_ls_waldir()));"""
    cleanup = json.loads(subprocess.check_output(
        args.compose+["exec", "-T", "postgres", "psql", "-U", "imu_admin", "-d", "imu", "-At", "-c", cleanup_sql],
        text=True, timeout=30))
    passed = bool(rows) and not failures and cleanup["pending_bytes"] == cleanup["payloads"] == cleanup["unfinished"] == 0
    if monitor_summary["unobserved_final_results"] or monitor_summary["observation_errors"]:
        passed = False
    summary = {"schema": "imu.replay.acceptance.v1", "mode": args.mode, "status": "PASS" if passed else "FAIL",
               "cases": len(rows), "failed_cases": len(failures), "wall_s": time.monotonic()-started,
               "replayed_input_seconds": sum(row.get("samples", 0) for row in rows)/25,
               "cohorts": dict(Counter(row["cohort"] for row in rows)),
               "steps_configurations": ["off", "on"] if args.mode in ("smoke", "matrix") else ["on"],
               "requested_upload_duration_s": args.seconds if args.mode == "soak" else None,
               "devices": args.devices if args.mode == "soak" else None,
               "cleanup": cleanup, "monitor": monitor_summary,
               "model_accuracy_claim": False, "data": "historical real records replayed; not new independent data",
               "calendar_clock": "synthetic test dates; not original participant dates or natural daily behavior",
               "soak_scope": "closed-loop clients; record completion precedes the next record; tail may exceed duration",
               "resource_config": "4 workers x 1 thread; dedicated Compose quotas",
               "manifest_sha256": references["manifest_sha256"],
               "reference_index_sha256": descriptor["reference_index_sha256"],
               "data_movement": "signals and record-level predictions remain on Ubuntu"}
    write_json(args.output/"summary.json", summary)
    lines = [f"# IMU replay {args.mode}", "", f"- Status: {summary['status']}",
             f"- Cases: {len(rows)}; failed: {len(failures)}",
             f"- Wall seconds: {summary['wall_s']:.2f}",
             "- Real historical inputs and frozen model; engineering equivalence, not new accuracy evidence.",
             "- See summary.json for latency distributions and database/resource measurements.",
             "- Detailed results remain on the source-data host.", ""]
    (args.output/"report.md").write_text("\n".join(lines))
    print(json.dumps(summary), flush=True)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
