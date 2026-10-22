"""Restore an in-flight synthetic job into a NEW isolated database and finish it."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from imu_inference import InferenceEngine, InputBatch
from imu_services.settings import Settings
from imu_services.store import Store, migrate
from imu_services.worker import Worker


class DeterministicBackend:
    def activity_probabilities(self, x, valid):
        return np.tile([.1, .2, .3, .4], (len(x), 1)).astype(np.float32)

    def step_gate(self, x, center):
        return .8

    def step_contacts(self, x):
        return np.flatnonzero(x[:, 0].astype(int) % 17 == 0), "OK"


def main():
    url = os.environ["IMU_TEST_DATABASE_URL"]
    suffix = uuid4().hex
    source, target = "recovery_source_"+suffix, "recovery_target_"+suffix
    names = [source, target]
    created = []
    stores = []
    started = time.monotonic()
    with psycopg.connect(url, autocommit=True) as admin:
        try:
            for name in names:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
            source_url, target_url = [make_conninfo(url, dbname=name) for name in names]
            migrate(source_url)
            config = Settings(source_url, "")
            store = Store(config)
            stores.append(store)
            run_id, _ = store.create(dict(request_key="restore-test", recording_id="synthetic",
                                          device_id="synthetic", start_time="2026-10-20T00:00:00Z",
                                          steps_enabled=True))
            x = np.column_stack((np.arange(3750), np.zeros(3750), np.full(3750, 9.8))).astype(np.float32)
            for start in range(0, len(x), 1500):
                store.accept(run_id, f"b{start}", InputBatch.create(x[start:start+1500], start=start, angular_velocity=np.zeros((len(x[start:start+1500]), 3), np.float32)))
            store.seal(run_id, len(x))
            backend = DeterministicBackend()
            worker = Worker(config, store, backend)
            worker.once()
            claim = store.claim(worker.engines)
            assert claim and claim["processed_samples"] == 1500
            creds = conninfo_to_dict(url)
            env = {**os.environ, **{f"PG{k.upper()}": v for k, v in creds.items()
                                    if k in ("host", "port", "user", "password", "sslmode")}}
            with tempfile.TemporaryDirectory(prefix="imu-restore-") as tmp:
                dump = Path(tmp)/"synthetic.dump"
                subprocess.run(["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-d", source,
                                "-f", str(dump)], env=env, check=True)
                restore_started = time.monotonic()
                subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                                "--single-transaction", "-d", target, str(dump)], env=env, check=True)
                restore_seconds = time.monotonic()-restore_started
                backup_bytes = dump.stat().st_size
            restored = Store(Settings(target_url, ""))
            stores.append(restored)
            # Stand in for the passage of lease time; this touches only our new test database.
            with restored.pool.connection() as c:
                before = restored._run(c, run_id)
                assert before["processed_samples"] == 1500 and before["status"] == "running"
                c.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
            recovery = Worker(restored.settings, restored, backend)
            for _ in range(10):
                recovery.once()
                with restored.pool.connection() as c:
                    after = restored._run(c, run_id)
                if after["status"] == "completed":
                    break
            expected = InferenceEngine(backend=backend).predict(x, angular_velocity=np.zeros((len(x), 3), np.float32))
            assert after["status"] == "completed" and after["summary"] == expected["statistics"]
            with restored.pool.connection() as c:
                assert c.execute("SELECT pending_bytes FROM admission_budget").fetchone()["pending_bytes"] == 0
                assert c.execute("SELECT count(*) AS n FROM input_batches WHERE payload IS NOT NULL").fetchone()["n"] == 0
                assert c.execute("SELECT count(*) AS n FROM results WHERE final").fetchone()["n"] == 1
                assert after["lease_token"] > before["lease_token"]
            report = dict(schema="imu.recovery.exercise.v1", passed=True, input="synthetic",
                          model_backend="deterministic transport-test backend; not an accuracy test",
                          restored_checkpoint_sample=1500, resumed_to_sample=len(x),
                          expired_lease_reclaimed=True, final_statistics_equal_full_run=True,
                          final_result_count=1, pending_input_payloads_cleared=True,
                          backup_bytes=backup_bytes, restore_seconds=restore_seconds,
                          total_seconds=time.monotonic()-started)
            output = Path("reports/recovery.json")
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps(report, indent=2))
        finally:
            for store in stores:
                store.close()
            for name in created:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
