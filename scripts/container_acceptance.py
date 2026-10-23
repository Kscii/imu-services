"""Exercise the actual Compose images, including worker SIGKILL and read-only query role."""
import argparse
import json
import gzip
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import httpx
import numpy as np
from imu_inference import InferenceEngine, PipelineConfig
from imu_services import __version__
from imu_services.analysis import activity_analysis
from imu_services.models import ActivityAnalysisRequest
from imu_services.readers import HttpResultReader


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-image", default=f"localhost/imu-runtime:{__version__}")
    parser.add_argument("--query-image", default=f"localhost/imu-query:{__version__}")
    parser.add_argument("--output", type=Path, default=Path(f"reports/container-acceptance-{__version__}.json"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    api_port, query_port = free_port(), free_port()
    settings = {key: secrets.token_hex(32) for key in (
        "IMU_ADMIN_DB_PASSWORD", "IMU_RUNTIME_DB_PASSWORD", "IMU_QUERY_DB_PASSWORD",
        "IMU_WRITE_TOKEN", "IMU_READ_TOKEN")}
    settings.update(IMU_RUNTIME_IMAGE=args.runtime_image,
                    IMU_QUERY_IMAGE=args.query_image, IMU_WORKERS="1",
                    IMU_THREADS="1", IMU_WORKER_CPUS="2", IMU_API_PORT=str(api_port),
                    IMU_QUERY_PORT=str(query_port), IMU_STEPS_ENABLED="true")
    with tempfile.TemporaryDirectory(prefix="imu-container-test-") as tmp:
        envfile = Path(tmp)/"test.env"
        envfile.write_text("\n".join(f"{k}={v}" for k, v in settings.items())+"\n")
        envfile.chmod(0o600)
        command = ["docker", "compose", "--env-file", str(envfile), "-p", "imu-acceptance-"+secrets.token_hex(5),
                   "-f", str(Path("compose.yaml").resolve())]
        if args.offline:
            command.extend(["-f", str(Path("compose.offline.yaml").resolve())])

        def compose(*args, **kw):
            return subprocess.run(command+list(args), check=True, **kw)

        started = time.monotonic()
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "300")
            api = httpx.Client(base_url=f"http://127.0.0.1:{api_port}",
                               headers={"Authorization": "Bearer "+settings["IMU_WRITE_TOKEN"]}, timeout=30)
            query = httpx.Client(base_url=f"http://127.0.0.1:{query_port}",
                                 headers={"Authorization": "Bearer "+settings["IMU_READ_TOKEN"]}, timeout=30)
            response = api.post("/v2/runs", json=dict(request_key="container-acceptance", recording_id="synthetic",
                                device_id="synthetic", start_time="2026-10-20T00:00:00Z"))
            response.raise_for_status()
            run_id = response.json()["run_id"]
            t = np.arange(3750)/25
            x = np.column_stack((.3*np.sin(t*2*np.pi*1.8), .15*np.cos(t*2*np.pi*1.8),
                                 9.80665+1.7*np.sin(t*2*np.pi*1.8))).astype(np.float32)
            valid = np.ones(len(x), bool)
            valid[800:825] = False
            body = dict(batch_id="first", start_sample=0, acceleration_mps2=x[:2250].tolist(),
                        angular_velocity_rads=np.zeros((2250, 3)).tolist(),
                        valid=valid[:2250].tolist())
            api.post(f"/v2/runs/{run_id}/batches", json=body).raise_for_status()

            def wait_for(predicate):
                limit = time.monotonic()+240
                while time.monotonic() < limit:
                    response = query.get(f"/v2/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    if run["status"] == "failed":
                        raise RuntimeError("container model run failed: "+str(run["last_error"]))
                    if predicate(run):
                        return run
                    time.sleep(.5)
                raise TimeoutError("container result did not progress")

            wait_for(lambda run: run["processed_samples"] == 2250)
            compose("kill", "-s", "SIGKILL", "worker")
            compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "worker")
            api.post(f"/v2/runs/{run_id}/batches",
                     json=dict(batch_id="tail", start_sample=2250, acceleration_mps2=x[2250:].tolist(),
                               angular_velocity_rads=np.zeros((len(x)-2250, 3)).tolist(),
                               valid=valid[2250:].tolist())).raise_for_status()
            api.post(f"/v2/runs/{run_id}/seal", json={"end_sample": len(x)}).raise_for_status()
            final = wait_for(lambda run: run["status"] == "completed")
            engine = InferenceEngine(threads=1)
            expected = engine.predict(x, valid=valid, angular_velocity=np.zeros((len(x), 3), np.float32))
            assert final["summary"] == expected["statistics"]
            assert final["provenance"] == expected["provenance"]
            rows = query.get(f"/v2/runs/{run_id}/results").json()["items"]
            assert sum(row["final"] for row in rows) == 1
            got_steps = [i for row in rows for event in row["events"] if event["kind"] == "step_segment"
                         for i in event["event_samples"]]
            want_steps = [i for event in expected["events"] if event["kind"] == "step_segment"
                          for i in event["event_samples"]]
            assert sorted(got_steps) == sorted(want_steps)
            assert api.post(f"/v2/runs/{run_id}/batches", json=body).json()["duplicate"]
            whole_x, whole_valid = np.tile(x, (4, 1)), np.tile(valid, 4)
            whole_header = dict(format="imu.recording.ndjson.v2", mode="whole_record",
                                request_key="whole-container-acceptance", recording_id="synthetic-whole",
                                device_id="synthetic", start_time="2026-10-20T00:00:00Z",
                                sample_rate_hz=25, sample_count=len(whole_x), steps_enabled=True)
            whole_body = (json.dumps(whole_header)+"\n"+"\n".join(
                json.dumps([*a.tolist(), 0., 0., 0., bool(v), False])
                for a, v in zip(whole_x, whole_valid, strict=True))+"\n").encode()
            whole = api.post("/v2/runs/whole-record", content=gzip.compress(whole_body),
                             headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip"})
            whole.raise_for_status()
            incremental_id, run_id = run_id, whole.json()["run_id"]
            first_attempt = wait_for(lambda run: run["status"] == "running")
            compose("kill", "-s", "SIGKILL", "worker-whole")
            compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "worker-whole")
            wait_for(lambda run: run["status"] == "running" and run["updated_at"] != first_attempt["updated_at"])
            # The incremental pool must progress while whole-record recomputation is active.
            whole_id = run_id
            probe = api.post("/v2/runs", json=dict(request_key="mixed-probe", recording_id="mixed-probe",
                             device_id="synthetic", start_time="2026-10-20T01:00:00Z", steps_enabled=False))
            probe.raise_for_status()
            run_id = probe.json()["run_id"]
            api.post(f"/v2/runs/{run_id}/batches", json=dict(batch_id="probe", start_sample=0,
                     acceleration_mps2=x[:500].tolist(), angular_velocity_rads=np.zeros((500, 3)).tolist())).raise_for_status()
            api.post(f"/v2/runs/{run_id}/seal", json={"end_sample": 500}).raise_for_status()
            wait_for(lambda run: run["status"] == "completed")
            run_id = whole_id
            whole_final = wait_for(lambda run: run["status"] == "completed")
            whole_engine = InferenceEngine(PipelineConfig(mode="whole_record"), threads=1,
                                           backend=engine.backend)
            whole_expected = whole_engine.predict(whole_x, angular_velocity=np.zeros_like(whole_x), valid=whole_valid)
            assert whole_final["summary"] == whole_expected["statistics"]
            assert whole_final["provenance"] == whole_expected["provenance"]
            assert whole_final["input_cleared"] and whole_final["diagnostics"]
            whole_rows = query.get(f"/v2/runs/{run_id}/results").json()["items"]
            assert [e for r in whole_rows for e in r["events"]] == whole_expected["events"]
            run_id = incremental_id
            calendar = query.get(f"/v2/runs/{run_id}/calendar",
                                 params=dict(timezone="UTC", date_from="2026-10-20", date_to="2026-10-20"))
            calendar.raise_for_status()
            assert calendar.json()["daily"][0]["observed_s"] == final["summary"]["observed_s"]
            body = ActivityAnalysisRequest(
                participant_id="synthetic-container", run_ids=[run_id, run_id], timezone="UTC",
                date_from="2026-10-20", date_to="2026-10-20")
            response = query.post("/v2/analysis/activity", json=body.model_dump(mode="json"))
            response.raise_for_status()
            analysis = response.json()
            assert analysis["duplicate_run_ids_removed"] == 1
            assert analysis["calendar"]["daily"][0]["observed_s"] == final["summary"]["observed_s"]
            assert calendar.json()["steps"] == analysis["calendar"]["steps"]
            assert analysis["calendar"]["steps"]["estimated_step_count"] == len(want_steps)
            assert analysis["calendar"]["steps"]["analyzed_s"] == final["summary"]["steps"]["analyzed_s"]
            whole_calendar = query.get(f"/v2/runs/{whole_id}/calendar", params=dict(
                timezone="UTC", date_from="2026-10-20", date_to="2026-10-20"))
            whole_calendar.raise_for_status()
            whole_analysis = query.post("/v2/analysis/activity", json=dict(
                participant_id="synthetic-whole", run_ids=[whole_id], timezone="UTC",
                date_from="2026-10-20", date_to="2026-10-20"))
            whole_analysis.raise_for_status()
            assert whole_calendar.json()["steps"] == whole_analysis.json()["calendar"]["steps"]
            assert whole_calendar.json()["steps"]["estimated_step_count"] == whole_expected["statistics"]["steps"]["estimated_step_count"]
            external_reader = HttpResultReader(f"http://127.0.0.1:{query_port}", settings["IMU_READ_TOKEN"])
            try:
                assert activity_analysis(external_reader, body) == analysis
            finally:
                external_reader.close()
            check = """
import importlib.util, os, psycopg
assert importlib.util.find_spec('torch') is None
with psycopg.connect(os.environ['IMU_DATABASE_URL']) as c:
    c.execute('SELECT count(*) FROM query_v1_results')
    try:
        c.execute('SELECT payload FROM input_batches')
    except psycopg.errors.InsufficientPrivilege:
        pass
    else:
        raise AssertionError('query role read raw inputs')
"""
            compose("exec", "-T", "query", "python", "-c", check)
            check_payloads = """
import os, psycopg
with psycopg.connect(os.environ['IMU_DATABASE_URL']) as c:
    assert c.execute('SELECT count(*) FROM input_batches WHERE payload IS NOT NULL').fetchone()[0] == 0
    assert c.execute('SELECT sum(pending_bytes) FROM admission_budget').fetchone()[0] == 0
"""
            compose("exec", "-T", "api", "python", "-c", check_payloads)
            report = dict(schema="imu.container.acceptance.v1", passed=True,
                          runtime_image=settings["IMU_RUNTIME_IMAGE"], query_image=settings["IMU_QUERY_IMAGE"],
                          real_frozen_models=True, gp2_enabled=True, worker_sigkill_recovery=True,
                          full_run_statistics_equal=True, exact_step_positions_equal=True,
                          duplicate_retry_after_cleanup=True, query_database_role_readonly=True,
                          query_has_no_torch=True, pending_payloads_cleared=True,
                          whole_record_exact_events=True, whole_record_sigkill_recovery=True,
                          whole_record_atomic_cleanup=True,
                          incremental_progress_during_whole_recovery=True,
                          analysis_http_reader_equal=True, analysis_duplicate_inputs_deduplicated=True,
                          calendar_steps_match_direct_events=True, both_modes_calendar_api_equal=True,
                          image_ids={ref: json.loads(subprocess.check_output(
                              ["docker", "image", "inspect", ref], text=True))[0]["Id"]
                              for ref in [args.runtime_image, args.query_image]},
                          provenance=final["provenance"], seconds=time.monotonic()-started,
                          estimated_steps_on_synthetic_fixture=final["summary"]["steps"]["estimated_step_count"])
            output = args.output
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps(report, indent=2), flush=True)
            api.close()
            query.close()
        except Exception:
            compose("logs", "--tail", "30", "api", "worker", "worker-whole", "query", "migrate")
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
