"""25-hour ingestion/queue/cleanup capacity with a deterministic test backend.

This exercises transport/storage limits, never classification accuracy or E7 CPU cost.
"""

import json
import os
import resource
import time
from pathlib import Path
from uuid import uuid4
import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from imu_services.ingest import RecordUpload
from imu_services.settings import Settings
from imu_services.store import Store, migrate
from imu_services.worker import Worker
from imu_services.readers import PostgresReader


class TransportBackend:
    def activity_probabilities(self, x, valid):
        return np.tile(np.array([0.1, 0.2, 0.6, 0.1], np.float32), (len(x), 1))

    def whole_record(self, x, valid, baseline, sign_adaptation):
        return baseline.argmax(1).astype(np.int8), dict(enabled=False, applied=False)


def main():
    url = os.environ["IMU_TEST_DATABASE_URL"]
    schema = "transport_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(url, options=f"-csearch_path={schema}")
    migrate(scoped)
    settings = Settings(scoped, "test", worker_mode="whole_record")
    store = Store(settings)
    u = RecordUpload(settings)
    memory = {}

    def mark(name):
        memory[name] = {
            k: int(v.split()[0])
            for k, v in (line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
            if k in ("VmRSS", "VmHWM")
        }

    mark("started")
    started = time.monotonic()
    try:
        header = dict(
            format="imu.recording.ndjson.v2",
            request_key=uuid4().hex,
            recording_id="synthetic",
            device_id="synthetic",
            start_time="2026-10-21T00:00:00Z",
            sample_count=2250000,
            steps_enabled=False,
        )
        u.feed((json.dumps(header) + "\n").encode())
        block = []
        for row in np.random.default_rng(25).normal(0, 0.1, (15000, 6)):
            row[1] += 9.80665
            block.append(json.dumps([*np.round(row, 4).tolist(), True, False], separators=(",", ":")) + "\n")
        payload = "".join(block).encode()
        for _ in range(150):
            u.feed(payload)
        mark("parsed")
        u.finish()
        parsed = time.monotonic()
        rid, duplicate = store.create_whole(u)
        accepted = time.monotonic()
        mark("accepted")
        assert not duplicate
        reader = PostgresReader(store)
        assert reader.results(rid) == []
        assert Worker(settings, store, TransportBackend()).once()
        row = reader.run(rid)
        done = time.monotonic()
        mark("completed")
        assert (
            row["status"] == "completed"
            and row["input_cleared"]
            and row["summary"]["through_sample"] == 2250000
        )
        with store.pool.connection() as c:
            assert c.execute("SELECT sum(pending_bytes) AS n FROM admission_budget").fetchone()["n"] == 0
            assert not c.execute("SELECT 1 FROM input_batches WHERE payload IS NOT NULL").fetchone()
        result = dict(
            schema="imu.whole.transport-capacity.v1",
            passed=True,
            samples=2250000,
            model="deterministic test backend; NOT E7 accuracy or compute benchmark",
            wire_bytes=u.raw_bytes,
            staged_bytes=u.spool.seek(0, 2),
            input_sha256=u.digest.hexdigest(),
            parse_seconds=parsed - started,
            accept_seconds=accepted - parsed,
            worker_seconds=done - accepted,
            memory=memory,
            peak_rss_kib=memory["completed"]["VmHWM"],
            getrusage_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        out = Path("reports/1.1.0/whole-25h-transport.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    finally:
        u.close()
        store.close()
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
