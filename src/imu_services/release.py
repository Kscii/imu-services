import hashlib
from importlib.metadata import version
import psycopg
from imu_inference import PipelineConfig
from imu_inference.contracts import canonical_json
from imu_inference.engine import artifact_provenance


def release_info():
    fingerprints = {}
    for mode, sign in (("incremental", False), ("whole_record", False), ("whole_record", True)):
        for steps in (False, True):
            provenance = artifact_provenance(PipelineConfig(mode=mode, sign_adaptation=sign, steps_enabled=steps))
            key = f"{mode}.sign_{int(sign)}.gp2_{'on' if steps else 'off'}"
            fingerprints[key] = hashlib.sha256(canonical_json(provenance)).hexdigest()
    return {"service_version": version("imu-services"), "library_version": version("imu-inference"),
            "http_major": 2, "database_schema": 2, "fingerprints": fingerprints}


def check_upgrade(url, *, steps_enabled=True, sign_adaptation=False):
    candidate = release_info()
    with psycopg.connect(url) as c:
        if not c.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]:
            return {"compatible": True, "reason": "new_database", **candidate}
        schema = c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        if schema not in (1, candidate["database_schema"]):
            raise RuntimeError("database schema requires an explicit compatible migration")
        condition = "status!='completed'" if schema == 1 else "status!='completed' AND NOT input_cleared"
        active = c.execute("SELECT DISTINCT fingerprint FROM runs WHERE "+condition).fetchall()
    supported = (set(candidate["fingerprints"].values()) if steps_enabled
                 else {v for k, v in candidate["fingerprints"].items() if k.endswith("gp2_off")})
    unsupported_sign = {v for k, v in candidate["fingerprints"].items()
                        if k.startswith("whole_record") and f".sign_{int(sign_adaptation)}." not in k}
    supported -= unsupported_sign
    unmatched = {row[0] for row in active}-supported
    if unmatched:
        raise RuntimeError("unfinished runs require the previous pipeline; seal/drain or replay before upgrading")
    return {"compatible": True, "reason": "active_run_fingerprints_supported", **candidate}
