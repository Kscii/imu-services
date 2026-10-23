"""Recipient configuration must preserve secrets and reject ambiguous image identity."""
import importlib.util
import json
from pathlib import Path
import stat

import pytest


spec = importlib.util.spec_from_file_location("create_env", Path(__file__).parents[1] / "scripts/create_env.py")
create_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(create_env)


@pytest.fixture
def deployment(tmp_path):
    source = Path(__file__).parents[1]
    for name in (".env.example", "release-versions.json"):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    versions = json.loads((tmp_path / "release-versions.json").read_text())
    manifest = dict(schema="imu.deployment.v1", **versions, images={
        name: f"ghcr.io/kscii/imu-services-{name}@sha256:" + "a" * 64 for name in ("runtime", "query")})
    (tmp_path / "deployment.json").write_text(json.dumps(manifest))
    return tmp_path, manifest


def test_online_pins_images_and_preserves_existing_secrets(deployment):
    root, manifest = deployment
    create_env.create_environment(root)
    path = root / ".env"
    before = path.read_bytes()
    settings = dict(line.split("=", 1) for line in before.decode().splitlines() if line and not line.startswith("#"))
    assert settings["IMU_RUNTIME_IMAGE"] == manifest["images"]["runtime"]
    assert settings["IMU_QUERY_IMAGE"] == manifest["images"]["query"]
    assert len(settings["IMU_WRITE_TOKEN"]) == 64
    assert settings["IMU_WRITE_TOKEN"] != settings["IMU_READ_TOKEN"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        create_env.create_environment(root)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["missing", "version", "tag", "newline", "other_registry", "wrong_target",
                                 "list_manifest", "list_images"])
def test_bad_manifest_does_not_create_environment(deployment, bad):
    root, manifest = deployment
    if bad == "missing":
        (root / "deployment.json").unlink()
    else:
        if bad == "list_manifest":
            manifest = []
        elif bad == "list_images":
            manifest["images"] = []
        elif bad == "version":
            manifest["library_version"] = "0.0.0"
        else:
            manifest["images"]["runtime"] = {
                "tag": "ghcr.io/kscii/imu-services-runtime:latest",
                "newline": manifest["images"]["runtime"] + "\nIMU_WRITE_TOKEN=injected",
                "other_registry": "example.com/imu@sha256:" + "a" * 64,
                "wrong_target": manifest["images"]["query"],
            }[bad]
        (root / "deployment.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        create_env.create_environment(root)
    assert not (root / ".env").exists()


def test_offline_needs_no_network_manifest(deployment):
    root, _ = deployment
    (root / "deployment.json").unlink()
    create_env.create_environment(root, "offline")
    assert "IMU_RUNTIME_IMAGE=localhost/imu-runtime:1.1.1\n" in (root / ".env").read_text()
