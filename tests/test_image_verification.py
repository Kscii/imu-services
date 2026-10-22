import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile

import pytest

verify = runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/verify_images.py"))["verify_saved_export"]


def fixture_export(*, tamper=False):
    config = json.dumps({"rootfs": {"diff_ids": ["sha256:expected-layer"]}, "config": {"User": "10001"}}).encode()
    digest = hashlib.sha256(config).hexdigest()
    reference = "localhost/imu-query:test"
    name = "blobs/sha256/"+digest
    entries = {
        name: config+b" " if tamper else config,
        "manifest.json": json.dumps([{"RepoTags": [reference], "Config": name, "Layers": []}]).encode(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for key, value in entries.items():
            info = tarfile.TarInfo(key)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    buffer.seek(0)
    return buffer, {reference: {"id": digest}}, {reference: {"RootFS": {"Layers": ["sha256:expected-layer"]}}}


def test_containerd_manifest_id_resolves_to_exact_config_bytes():
    stream, expected, inspected = fixture_export()
    assert verify(stream, expected, inspected) == {tag: row["id"] for tag, row in expected.items()}


def test_export_does_not_trust_config_filename_or_wrong_layers():
    stream, expected, inspected = fixture_export(tamper=True)
    with pytest.raises(RuntimeError, match="config digest"):
        verify(stream, expected, inspected)
    stream, expected, inspected = fixture_export()
    inspected[next(iter(inspected))]["RootFS"]["Layers"] = ["sha256:other-layer"]
    with pytest.raises(RuntimeError, match="layer identity"):
        verify(stream, expected, inspected)
