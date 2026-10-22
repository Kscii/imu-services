"""One model-bearing process. Multiple processes claim independent stream runs."""
import hashlib
import logging
import os
from pathlib import Path
import signal
import threading
import time
import numpy as np
from uuid import uuid4
from imu_inference import InferenceEngine, PipelineConfig
from imu_inference.contracts import canonical_json
from .codec import decode_batch, canonical_rows
from .settings import Settings
from .store import Store, LostLease

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, store=None, backend=None):
        self.settings = settings
        self.store = store or Store(settings)
        self.stop = threading.Event()
        self.worker_id = uuid4()
        self.last_seen = 0.
        config = dict(mode=settings.worker_mode,
                      sign_adaptation=settings.sign_adaptation if settings.worker_mode == "whole_record" else False)
        base = InferenceEngine(PipelineConfig(steps_enabled=settings.steps_enabled, **config),
                               threads=settings.threads, backend=backend)
        self.engines = {base.fingerprint: base}
        if settings.steps_enabled:
            disabled = InferenceEngine(PipelineConfig(steps_enabled=False, **config),
                                       threads=settings.threads, backend=base.backend)
            self.engines[disabled.fingerprint] = disabled

    def seen(self):
        self.store.worker_seen(self.worker_id, self.engines)
        health_file = os.environ.get("IMU_WORKER_HEALTH_FILE")
        if health_file:
            Path(health_file).touch()

    def once(self):
        if time.monotonic()-self.last_seen > 5:
            self.seen()
            self.last_seen = time.monotonic()
        run = self.store.claim(self.engines, self.settings.worker_mode)
        if run is None:
            return False
        token, run_id = run["lease_token"], run["id"]
        done = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not done.wait(max(.2, self.settings.lease_seconds/3)):
                try:
                    self.seen()
                    if not self.store.heartbeat(run_id, token):
                        lost.set()
                        return
                except Exception:
                    log.warning("heartbeat unavailable", extra={"run_id": str(run_id)})

        heart = threading.Thread(target=heartbeat, daemon=True)
        heart.start()
        try:
            engine = self.engines[run["fingerprint"]]
            if hashlib.sha256(canonical_json(run["provenance"])).hexdigest() != engine.fingerprint:
                raise RuntimeError("stored provenance mismatch")
            if run["mode"] == "whole_record":
                self.complete_record(run, engine, lost)
                return True
            stream = engine.restore(run["checkpoint"]) if run["checkpoint"] else engine.stream()
            rows, start, end = self.store.inputs(run)
            cursor, events = start, []
            for row in rows:
                if row["payload"] is None:
                    raise RuntimeError("pending input payload was removed")
                batch = decode_batch(row["payload"])
                if batch.digest != row["digest"]:
                    raise RuntimeError("input checksum mismatch")
                lo, hi = max(cursor, batch.start), min(end, batch.end)
                if lo != cursor:
                    raise RuntimeError("unexpected gap below completeness frontier")
                if hi > lo:
                    events.extend(stream.append(batch.slice(lo, hi)))
                    cursor = hi
            if cursor != end:
                raise RuntimeError("incomplete stored input")
            final = run["sealed_end"] is not None and end == run["sealed_end"]
            if final:
                events.extend(stream.finish())
            if lost.is_set():
                raise LostLease("lease lost during computation")
            self.store.commit(run_id, token, end, stream.checkpoint(), events,
                              stream.statistics.result(engine.config.steps_enabled), final)
        except LostLease:
            log.warning("discarded stale worker output", extra={"run_id": str(run_id)})
        except Exception as exc:
            log.exception("computation failed", extra={"run_id": str(run_id)})
            try:
                self.store.fail(run_id, token, type(exc).__name__)
            except LostLease:
                pass
        finally:
            done.set()
            heart.join(timeout=5)
        return True

    def complete_record(self, run, engine, lost):
        rows, start, end = self.store.inputs(run)
        if start != 0 or end != run["sealed_end"]:
            raise RuntimeError("invalid complete-record bounds")
        values, valid, excluded = np.empty((end, 6), np.float32), np.empty(end, bool), np.empty(end, bool)
        digest, cursor = hashlib.sha256(), 0
        for row in rows:
            if row["payload"] is None:
                raise RuntimeError("complete-record input removed")
            batch = decode_batch(row["payload"])
            if batch.digest != row["digest"] or batch.start != cursor or batch.end > end:
                raise RuntimeError("complete-record checksum or continuity mismatch")
            digest.update(canonical_rows(batch))
            values[cursor:batch.end] = batch.values
            valid[cursor:batch.end], excluded[cursor:batch.end] = batch.valid, batch.excluded
            cursor = batch.end
        del rows
        if cursor != end or digest.hexdigest() != run["input_sha256"]:
            raise RuntimeError("complete-record digest mismatch")
        result = engine.predict(values[:, :3], angular_velocity=values[:, 3:], valid=valid, excluded=excluded)
        if lost.is_set():
            raise LostLease("lease lost during complete-record computation")
        self.store.commit_whole(run["id"], run["lease_token"], result)

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop.set())
        try:
            while not self.stop.is_set():
                if not self.once():
                    self.stop.wait(self.settings.poll_seconds)
        finally:
            self.store.close()


def main():
    logging.basicConfig(level=logging.INFO)
    Worker(Settings.from_env("worker")).run()
