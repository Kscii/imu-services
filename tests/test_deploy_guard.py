"""Fault-inject the deployment control flow using a fake Docker command."""

import os
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.parametrize("migrated", [False, True])
def test_deploy_failure_stops_candidate_and_controls_schema_rollback(tmp_path, migrated):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "operations").mkdir()
    shutil.copy2(root / "operations/deploy.sh", tmp_path / "operations/deploy.sh")
    old_env = "IMU_RUNTIME_IMAGE=old-runtime\nIMU_QUERY_IMAGE=old-query\nIMU_ADMIT_NEW_RUNS=true\n"
    (tmp_path / ".env").write_text(old_env)
    (tmp_path / "schema").write_text("1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['TEST_DEPLOY_ROOT']);args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':print('a'*40)
if 'psql' in args:print((root/'schema').read_text())
if 'pg_dump' in args:print('test-only backup')
if args[:2]==['compose','run'] and args[-1]=='migrate' and os.environ['TEST_SCHEMA_CHANGE']=='yes':(root/'schema').write_text('2')
if args[:2]==['compose','up'] and '--wait-timeout' in args:raise SystemExit(99)
""")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        "TEST_DEPLOY_ROOT": str(tmp_path),
        "TEST_SCHEMA_CHANGE": "yes" if migrated else "no",
    }
    digest = "b" * 64
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "operations/deploy.sh"),
            "ghcr.io/company/imu-runtime@sha256:" + digest,
            "ghcr.io/company/imu-query@sha256:" + digest,
            "a" * 40,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    calls = [__import__("json").loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    failed = next(i for i, c in enumerate(calls) if "--wait-timeout" in c)
    assert any(c[:2] == ["compose", "stop"] for c in calls[failed + 1 :])
    if migrated:
        assert "IMU_ADMIT_NEW_RUNS=false" in (tmp_path / ".env").read_text()
        assert not any(c[:2] == ["compose", "up"] for c in calls[failed + 1 :])
        assert "Schema changed" in result.stderr
    else:
        assert (tmp_path / ".env").read_text() == old_env
        assert calls[-1][:3] == ["compose", "up", "-d"]
    assert len(list((tmp_path / "backups").glob("*.dump"))) == 1
