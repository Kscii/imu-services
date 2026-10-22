"""PostgreSQL admission, leased work and immutable result commits."""
from __future__ import annotations
import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from imu_inference import PipelineConfig
from imu_inference.contracts import canonical_json
from imu_inference.engine import artifact_provenance
from .codec import encode_batch
from .errors import Conflict as Conflict, NotFound as NotFound, Overloaded as Overloaded, LostLease as LostLease


def migrate(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4803925)")
        exists = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]
        if exists:
            current = conn.execute("SELECT coalesce(max(version),0) FROM schema_migrations").fetchone()[0]
            if current > 2:
                raise RuntimeError("database schema is newer than this service")
            if current == 2:
                return
            if current == 1:
                if conn.execute("SELECT 1 FROM runs WHERE status!='completed' LIMIT 1").fetchone():
                    raise RuntimeError("drain all v1 tasks before migrating to the six-axis v2 contract")
                conn.execute((Path(__file__).parent/"migration_v2.sql").read_text())
        elif conn.execute("SELECT to_regclass('runs')").fetchone()[0]:
            raise RuntimeError("refusing to alter an unmanaged existing runs table")
        conn.execute((Path(__file__).parent/"schema.sql").read_text())


class Store:
    def __init__(self, settings):
        self.settings = settings
        def configure(c):
            c.execute("SET statement_timeout='10s'")
            c.execute("SET lock_timeout='3s'")
            c.execute("SET idle_in_transaction_session_timeout='15s'")
            c.commit()
        self.pool = ConnectionPool(settings.database_url, min_size=1, max_size=settings.pool_size,
                                   kwargs={"row_factory": dict_row}, timeout=5, max_waiting=64,
                                   configure=configure, open=True)
        self.pool.wait(timeout=10)

    def close(self):
        self.pool.close()

    def ready(self):
        with self.pool.connection() as c:
            return c.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] == 2

    def worker_seen(self, worker_id, fingerprints):
        with self.pool.connection() as c:
            c.execute("""INSERT INTO worker_heartbeats(id,fingerprints) VALUES(%s,%s)
                ON CONFLICT(id) DO UPDATE SET fingerprints=EXCLUDED.fingerprints,last_seen=now()""",
                      (worker_id, list(fingerprints)))

    @staticmethod
    def _run(c, run_id, lock=False):
        row = c.execute("SELECT * FROM runs WHERE id=%s"+(" FOR UPDATE" if lock else ""),
                        (run_id,)).fetchone()
        if row is None:
            raise NotFound("run not found")
        return row

    def create(self, request):
        request = dict(request)
        request.setdefault("mode", "incremental")
        if request["mode"] != "incremental":
            raise ValueError("use the complete-record upload endpoint for whole_record mode")
        requested_steps = request.pop("steps_enabled")
        # Idempotency is tied to the submitted request, not defaults of a later deployment.
        digest = hashlib.sha256(canonical_json({**request, "steps_enabled": requested_steps})).hexdigest()
        config = PipelineConfig(steps_enabled=self.settings.steps_enabled
                                if requested_steps is None else requested_steps)
        provenance = artifact_provenance(config)
        fingerprint = hashlib.sha256(canonical_json(provenance)).hexdigest()
        with self.pool.connection() as c:
            c.execute("SELECT id FROM admission_budget WHERE id=1 FOR UPDATE")
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 4803926))", (request["request_key"],))
            old = c.execute("SELECT * FROM runs WHERE request_key=%s", (request["request_key"],)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("request key already used with different parameters")
                return old["id"], True
            if not self.settings.admit_new_runs:
                raise Overloaded("maintenance: new task admission is paused")
            if config.steps_enabled and not self.settings.steps_enabled:
                raise Conflict("GP2 disabled by deployment configuration")
            active = c.execute("SELECT count(*) AS n FROM runs WHERE mode='incremental' AND status!='completed'").fetchone()["n"]
            if active >= self.settings.max_open_runs:
                raise Overloaded("open run limit reached")
            run_id = uuid4()
            c.execute("""INSERT INTO runs(id,request_key,request_hash,recording_id,device_id,
                         start_time,config,provenance,fingerprint)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (run_id, request["request_key"], digest, request["recording_id"],
                       request["device_id"], request["start_time"], Jsonb(config.__dict__),
                       Jsonb(provenance), fingerprint))
            return run_id, False

    def create_whole(self, upload):
        request = upload.header.model_dump(mode="json")
        input_hash = upload.digest.hexdigest()
        digest = hashlib.sha256(canonical_json(dict(request=request, input_sha256=input_hash))).hexdigest()
        requested_steps = request["steps_enabled"]
        config = PipelineConfig(mode="whole_record", sign_adaptation=self.settings.sign_adaptation,
                                steps_enabled=self.settings.steps_enabled
                                if requested_steps is None else requested_steps)
        provenance = artifact_provenance(config)
        fingerprint = hashlib.sha256(canonical_json(provenance)).hexdigest()
        with self.pool.connection() as c:
            budget = c.execute("SELECT pending_bytes FROM admission_budget WHERE id=2 FOR UPDATE").fetchone()
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 4803926))", (request["request_key"],))
            old = c.execute("SELECT * FROM runs WHERE request_key=%s", (request["request_key"],)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("request key already used with different content")
                return old["id"], True
            if not self.settings.admit_new_runs:
                raise Overloaded("maintenance: new task admission is paused")
            if config.steps_enabled and not self.settings.steps_enabled:
                raise Conflict("GP2 disabled by deployment configuration")
            active = c.execute("SELECT count(*) AS n FROM runs WHERE mode='whole_record' "
                               "AND status NOT IN ('completed','failed')").fetchone()["n"]
            if active >= self.settings.max_whole_runs:
                raise Overloaded("complete-record queue is full")
            run_id, end = uuid4(), upload.count
            c.execute("""INSERT INTO runs(id,request_key,request_hash,recording_id,device_id,start_time,
                         config,provenance,fingerprint,mode,input_sha256,available_end,sealed_end,status)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'whole_record',%s,%s,%s,'queued')""",
                      (run_id, request["request_key"], digest, request["recording_id"], request["device_id"],
                       request["start_time"], Jsonb(config.__dict__), Jsonb(provenance), fingerprint,
                       input_hash, end, end))
            pending = budget["pending_bytes"]
            for batch in upload.batches():
                payload = encode_batch(batch)
                pending += len(payload)
                if pending > self.settings.max_whole_pending_bytes:
                    raise Overloaded("complete-record input budget reached")
                c.execute("""INSERT INTO input_batches(run_id,batch_id,start_sample,end_sample,digest,
                             payload,payload_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                          (run_id, str(batch.start), batch.start, batch.end, batch.digest, payload, len(payload)))
            c.execute("UPDATE admission_budget SET pending_bytes=%s WHERE id=2", (pending,))
            return run_id, False

    def accept(self, run_id, batch_id, batch):
        if len(batch.acceleration) > self.settings.max_batch_samples:
            raise ValueError("batch sample limit exceeded")
        digest, payload = batch.digest, encode_batch(batch)
        with self.pool.connection() as c:
            budget = c.execute("SELECT pending_bytes FROM admission_budget WHERE id=1 FOR UPDATE").fetchone()
            run = self._run(c, run_id, True)
            if run["mode"] != "incremental":
                raise Conflict("complete-record input is immutable; resubmit with a new request key")
            old = c.execute("SELECT * FROM input_batches WHERE run_id=%s AND batch_id=%s",
                            (run_id, batch_id)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("batch id reused with different content")
                return dict(batch_id=old["batch_id"], duplicate=True, available_end=run["available_end"])
            overlaps = c.execute("""SELECT batch_id,start_sample,end_sample,digest FROM input_batches
                                    WHERE run_id=%s AND start_sample<%s AND end_sample>%s LIMIT 2""",
                                 (run_id, batch.end, batch.start)).fetchall()
            if overlaps:
                old = overlaps[0]
                if (len(overlaps) == 1 and old["start_sample"] == batch.start
                        and old["end_sample"] == batch.end and old["digest"] == digest):
                    return dict(batch_id=old["batch_id"], duplicate=True, available_end=run["available_end"])
                raise Conflict("overlapping input; retry the original immutable batch")
            if run["sealed_end"] is not None or run["status"] == "completed":
                raise Conflict("sealed run accepts identical retries only; use a new run for revisions")
            pending = c.execute("""SELECT count(*) AS n,coalesce(sum(end_sample-start_sample),0) AS samples
                                   FROM input_batches WHERE run_id=%s AND payload IS NOT NULL""",
                                (run_id,)).fetchone()
            if (budget["pending_bytes"]+len(payload) > self.settings.max_pending_bytes
                    or pending["samples"]+len(batch.acceleration) > self.settings.max_pending_samples_per_run
                    or pending["n"] >= self.settings.max_pending_batches_per_run):
                raise Overloaded("pending input budget reached; retry after processing")
            c.execute("""INSERT INTO input_batches(run_id,batch_id,start_sample,end_sample,digest,payload,
                         payload_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                      (run_id, batch_id, batch.start, batch.end, digest, payload, len(payload)))
            c.execute("UPDATE admission_budget SET pending_bytes=pending_bytes+%s WHERE id=1", (len(payload),))
            frontier = run["available_end"]
            if batch.start == frontier:
                rows = c.execute("""SELECT start_sample,end_sample FROM input_batches
                                    WHERE run_id=%s AND start_sample>=%s ORDER BY start_sample""",
                                 (run_id, frontier)).fetchall()
                for r in rows:
                    if r["start_sample"] != frontier:
                        break
                    frontier = r["end_sample"]
            status = run["status"]
            if status == "waiting" and frontier-run["processed_samples"] >= self.settings.minimum_ready_samples:
                status = "queued"
            c.execute("UPDATE runs SET available_end=%s,status=%s,updated_at=now() WHERE id=%s",
                      (frontier, status, run_id))
            return dict(batch_id=batch_id, duplicate=False, available_end=frontier)

    def seal(self, run_id, end):
        with self.pool.connection() as c:
            run = self._run(c, run_id, True)
            if run["sealed_end"] is not None:
                if run["sealed_end"] != end:
                    raise Conflict("recording end is immutable")
                return
            highest = c.execute("SELECT coalesce(max(end_sample),0) AS n FROM input_batches WHERE run_id=%s",
                                (run_id,)).fetchone()["n"]
            if run["available_end"] != end or highest != end:
                raise Conflict("incomplete input; submit missing data or explicit gap ranges before sealing")
            status = "queued" if run["status"] == "waiting" else run["status"]
            c.execute("UPDATE runs SET sealed_end=%s,status=%s,updated_at=now() WHERE id=%s",
                      (end, status, run_id))

    def retry(self, run_id):
        with self.pool.connection() as c:
            run = self._run(c, run_id, True)
            if run["input_cleared"]:
                raise Conflict("input was cleared; resubmit the complete recording with a new request key")
            if run["status"] != "failed":
                raise Conflict("only failed runs need explicit retry")
            c.execute("""UPDATE runs SET status='queued',failures=0,next_attempt=now(),last_error=NULL,
                         updated_at=now() WHERE id=%s""", (run_id,))

    def claim(self, fingerprints, mode="incremental"):
        with self.pool.connection() as c:
            if mode == "whole_record":
                c.execute("SELECT id FROM admission_budget WHERE id=2 FOR UPDATE")
            row = c.execute("""SELECT * FROM runs
                WHERE fingerprint=ANY(%s) AND mode=%s AND next_attempt<=now() AND
                (status='queued' OR (status='running' AND lease_until<now()))
                ORDER BY updated_at,id FOR UPDATE SKIP LOCKED LIMIT 1""", (list(fingerprints), mode)).fetchone()
            if row is None:
                return None
            failures = row["failures"]+(row["status"] == "running")
            if failures >= self.settings.max_failures:
                c.execute("""UPDATE runs SET status='failed',failures=%s,last_error='LEASE_RETRY_LIMIT',
                             lease_until=NULL,updated_at=now() WHERE id=%s""", (failures, row["id"]))
                if mode == "whole_record":
                    self._clear_whole(c, row["id"])
                return None
            token = row["lease_token"]+1
            row = c.execute("""UPDATE runs SET status='running',lease_token=%s,failures=%s,
                lease_until=now()+make_interval(secs => %s),updated_at=now() WHERE id=%s RETURNING *""",
                            (token, failures, self.settings.lease_seconds, row["id"])).fetchone()
            c.execute("INSERT INTO attempt_events(run_id,lease_token,event) VALUES(%s,%s,'claimed')",
                      (row["id"], token))
            return row

    def heartbeat(self, run_id, token):
        with self.pool.connection() as c:
            return c.execute("""UPDATE runs SET lease_until=now()+make_interval(secs => %s)
                WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()""",
                             (self.settings.lease_seconds, run_id, token)).rowcount == 1

    def inputs(self, run):
        start = run["processed_samples"]
        end = (run["available_end"] if run["mode"] == "whole_record"
               else min(run["available_end"], start+self.settings.processing_quantum))
        with self.pool.connection() as c:
            rows = c.execute("""SELECT start_sample,end_sample,payload,digest FROM input_batches
                                WHERE run_id=%s AND end_sample>%s AND start_sample<%s
                                ORDER BY start_sample""", (run["id"], start, end)).fetchall()
        return rows, start, end

    @staticmethod
    def _owns(c, run_id, token):
        run = c.execute("""SELECT * FROM runs WHERE id=%s AND lease_token=%s AND status='running'
                           AND lease_until>now() FOR UPDATE""", (run_id, token)).fetchone()
        if run is None:
            raise LostLease("lease lost; discard computation")
        return run

    def commit(self, run_id, token, end, checkpoint, events, summary, final):
        with self.pool.connection() as c:
            c.execute("SELECT id FROM admission_budget WHERE id=1 FOR UPDATE")
            run = self._owns(c, run_id, token)
            if run["mode"] != "incremental":
                raise Conflict("whole_record requires an atomic complete-record commit")
            if end < run["processed_samples"] or end > run["available_end"]:
                raise Conflict("invalid processing frontier")
            if final and run["sealed_end"] != end:
                raise Conflict("only explicitly sealed input can finish")
            if checkpoint["state"]["received"] != end or checkpoint["state"]["fingerprint"] != run["fingerprint"]:
                raise Conflict("checkpoint provenance or position mismatch")
            pending = run["pending_events"]+events
            publish = final or any(e["kind"] == "activity_core" for e in events)
            if publish:
                content = dict(provenance=run["provenance"], events=pending, summary=summary, final=final)
                digest = hashlib.sha256(canonical_json(content)).hexdigest()
                c.execute("""INSERT INTO results(run_id,start_sample,end_sample,final,provenance,events,
                    summary,content_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (run_id, run["published_samples"], end, final, Jsonb(run["provenance"]),
                           Jsonb(pending), Jsonb(summary), digest))
                pending = []
            if final:
                status = "completed"
            elif (run["available_end"]-end >= self.settings.minimum_ready_samples
                  or run["sealed_end"] is not None):
                status = "queued"
            else:
                status = "waiting"
            c.execute("""UPDATE runs SET processed_samples=%s,checkpoint=%s,pending_events=%s,summary=%s,
                         status=%s,lease_until=NULL,failures=0,last_error=NULL,next_attempt=now(),
                         published_samples=%s,updated_at=now() WHERE id=%s""",
                      (end, Jsonb(checkpoint), Jsonb(pending), Jsonb(summary), status,
                       end if publish else run["published_samples"], run_id))
            freed = c.execute("""SELECT coalesce(sum(payload_bytes),0) AS n FROM input_batches
                WHERE run_id=%s AND payload IS NOT NULL AND end_sample<=%s""", (run_id, end)).fetchone()["n"]
            if freed:
                c.execute("""UPDATE input_batches SET payload=NULL,payload_bytes=0
                    WHERE run_id=%s AND payload IS NOT NULL AND end_sample<=%s""", (run_id, end))
                c.execute("UPDATE admission_budget SET pending_bytes=pending_bytes-%s WHERE id=1", (freed,))
            c.execute("INSERT INTO attempt_events(run_id,lease_token,event) VALUES(%s,%s,%s)",
                      (run_id, token, "completed" if final else "checkpointed"))

    def fail(self, run_id, token, code):
        with self.pool.connection() as c:
            mode = self._run(c, run_id)["mode"]
            if mode == "whole_record":
                c.execute("SELECT id FROM admission_budget WHERE id=2 FOR UPDATE")
            run = self._owns(c, run_id, token)
            failures = run["failures"]+1
            status = "failed" if failures >= self.settings.max_failures else "queued"
            c.execute("""UPDATE runs SET failures=%s,status=%s,last_error=%s,lease_until=NULL,
                         next_attempt=now()+make_interval(secs => %s),updated_at=now() WHERE id=%s""",
                      (failures, status, code[:200], min(60, 2**failures), run_id))
            c.execute("INSERT INTO attempt_events(run_id,lease_token,event,detail) VALUES(%s,%s,'failed',%s)",
                      (run_id, token, code[:200]))
            if status == "failed" and mode == "whole_record":
                self._clear_whole(c, run_id)

    @staticmethod
    def _clear_whole(c, run_id):
        freed = c.execute("SELECT coalesce(sum(payload_bytes),0) AS n FROM input_batches WHERE run_id=%s",
                          (run_id,)).fetchone()["n"]
        c.execute("UPDATE input_batches SET payload=NULL,payload_bytes=0 WHERE run_id=%s", (run_id,))
        c.execute("UPDATE admission_budget SET pending_bytes=pending_bytes-%s WHERE id=2", (freed,))
        c.execute("UPDATE runs SET input_cleared=true,checkpoint=NULL,pending_events='[]' WHERE id=%s", (run_id,))

    def commit_whole(self, run_id, token, result):
        with self.pool.connection() as c:
            c.execute("SELECT id FROM admission_budget WHERE id=2 FOR UPDATE")
            run = self._owns(c, run_id, token)
            end = run["sealed_end"]
            summary = result["statistics"]
            if (run["mode"] != "whole_record" or end != run["available_end"]
                    or summary["through_sample"] != end or not summary["finished"]
                    or result["provenance"] != run["provenance"]):
                raise Conflict("complete-record output identity or coverage mismatch")
            buckets = [[] for _ in range((end+1499)//1500)]
            for event in result["events"]:
                start = event.get("start", event.get("first", 0))
                buckets[min(int(start)//1500, len(buckets)-1)].append(event)
            # All pages become visible together at this transaction's commit.
            for i, events in enumerate(buckets):
                start, stop = i*1500, min((i+1)*1500, end)
                content = dict(provenance=run["provenance"], events=events, summary=summary, final=stop == end)
                digest = hashlib.sha256(canonical_json(content)).hexdigest()
                c.execute("""INSERT INTO results(run_id,start_sample,end_sample,final,provenance,events,
                             summary,content_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (run_id, start, stop, stop == end, Jsonb(run["provenance"]), Jsonb(events),
                           Jsonb(summary), digest))
            c.execute("""UPDATE runs SET status='completed',processed_samples=%s,published_samples=%s,
                         summary=%s,diagnostics=%s,lease_until=NULL,failures=0,last_error=NULL,
                         updated_at=now() WHERE id=%s""",
                      (end, end, Jsonb(summary), Jsonb(result.get("diagnostics", {})), run_id))
            self._clear_whole(c, run_id)
            c.execute("INSERT INTO attempt_events(run_id,lease_token,event) VALUES(%s,%s,'completed')",
                      (run_id, token))
