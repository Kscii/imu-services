"""Build direct-library goldens on the source-data host, outside the HTTP service."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import multiprocessing
from pathlib import Path
import time

from imu_services.replay import load_records, load_input, write_json

ENGINES = None


def initialize():
    global ENGINES
    from imu_inference import InferenceEngine, PipelineConfig
    enabled = InferenceEngine(threads=1)
    ENGINES = {True: enabled, False: InferenceEngine(PipelineConfig(steps_enabled=False),
                                                    backend=enabled.backend)}


def reference(item):
    record, directory, include_gap = item
    batch = load_input(record)
    configs = {}
    for gap in ([False, True] if include_gap else [False]):
        x, valid, excluded = batch.acceleration.copy(), batch.valid.copy(), batch.excluded.copy()
        gyro = None if batch.angular_velocity is None else batch.angular_velocity.copy()
        if gap:
            x[1000:1250], valid[1000:1250], excluded[1000:1250] = 0, False, False
            if gyro is not None:
                gyro[1000:1250] = 0
        for enabled, engine in ENGINES.items():
            suffix = "-gap1000-1250" if gap else ""
            filename = f"{record['sha256']}{suffix}-{engine.fingerprint}.json"
            path = Path(directory)/filename
            if not path.exists():
                result = engine.predict(x, valid=valid, excluded=excluded, angular_velocity=gyro)
                write_json(path, result)
            else:
                result = json.loads(path.read_text())
                if result["provenance"] != engine.provenance:
                    raise ValueError("cached reference provenance mismatch")
            configs[("on" if enabled else "off")+("-gap" if gap else "")] = {
                "file": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "fingerprint": engine.fingerprint,
            }
    return {"alias": record["alias"], "input_sha256": record["sha256"],
            "cohort": record["cohort"], "samples": record["samples"], "configs": configs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2, 4))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    records, completed = load_records(args.manifest), []
    longest = {max((r for r in records if r["cohort"] == cohort), key=lambda r: r["samples"])["alias"]
               for cohort in {r["cohort"] for r in records}}
    started = time.monotonic()
    with ProcessPoolExecutor(args.workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=initialize) as pool:
        futures = [pool.submit(reference, (r, args.output, r["alias"] in longest)) for r in records]
        for future in as_completed(futures):
            completed.append(future.result())
            write_json(args.output/"progress.json", {"completed": len(completed), "total": len(records)})
            print(f"direct references completed {len(completed)}/{len(records)}", flush=True)
    index = {
        "schema": "imu.replay.references.v1", "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "records": sorted(completed, key=lambda r: r["alias"]), "seconds": time.monotonic()-started,
        "reference_scope": "direct fixed library, both GP2 configurations; not new labeled accuracy",
    }
    index_path = args.output/"index.json"
    previous = json.loads(index_path.read_text()) if index_path.exists() else None
    # Client sessions pin the index digest. A cache-only run must not invalidate
    # their resume state merely because the reference scan took a different time.
    identity_keys = ("schema", "manifest_sha256", "records", "reference_scope")
    if previous is None or any(previous.get(key) != index[key] for key in identity_keys):
        write_json(index_path, index)
    write_json(args.output/"summary.json", {
        "status": "PASS", "records": len(completed), "samples": sum(r["samples"] for r in completed),
        "configurations_per_record": 2, "seconds": time.monotonic()-started,
        "reference_configurations": sum(len(r["configs"]) for r in completed),
        "timing_scope": "generation or cache scan; not an inference benchmark",
        "data_movement": "raw data and record-level predictions stay on the source host",
    })


if __name__ == "__main__":
    main()
