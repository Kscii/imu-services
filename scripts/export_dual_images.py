"""Export verified local delivery images and check archive configs and every layer digest."""

import argparse
import gzip
import hashlib
import json
import subprocess
import tarfile
import zlib
from pathlib import Path


def inspect(ref):
    return json.loads(subprocess.check_output(["docker", "image", "inspect", ref], text=True))[0]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--verification", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    verified = json.loads(args.verification.read_text())
    if not verified["passed"] or verified["library_version"] != "1.0.0":
        raise ValueError("verify candidate first")
    refs = {r["reference"]: inspect(r["reference"]) for r in verified["images"]}
    for row in verified["images"]:
        assert refs[row["reference"]]["Id"] == row["engine_image_id"]
    postgres = "localhost/imu-postgres:18-20261020"
    refs[postgres] = inspect(postgres)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "imu-images-1.1.0.tar.gz"
    if target.exists():
        raise FileExistsError("refusing to replace an existing export")
    partial = target.with_suffix(".gz.partial")
    proc = subprocess.Popen(["docker", "image", "save", *refs], stdout=subprocess.PIPE)
    try:
        with gzip.open(partial, "wb", compresslevel=1) as f:
            while data := proc.stdout.read(1024 * 1024):
                f.write(data)
        if proc.wait() != 0:
            raise RuntimeError("image export failed")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
    docs = {}
    layer_digests = {}
    manifest = None
    with tarfile.open(partial, "r|gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            stream = tar.extractfile(member)
            if member.size < 1024 * 1024:
                data = stream.read()
                try:
                    value = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    value = None
                if member.name == "manifest.json":
                    manifest = value
                if isinstance(value, dict) and "rootfs" in value:
                    docs[member.name] = (hashlib.sha256(data).hexdigest(), value)
                    continue
                # Small tar layers must still be checked below.
                chunks = [data]
            else:
                chunks = []
            raw = hashlib.sha256()
            uncompressed = hashlib.sha256()
            decoder = None
            first = True
            while chunks or (data := stream.read(1024 * 1024)):
                if chunks:
                    data = chunks.pop()
                if first and data[:2] == b"\x1f\x8b":
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                first = False
                raw.update(data)
                uncompressed.update(decoder.decompress(data) if decoder else data)
            if decoder:
                uncompressed.update(decoder.flush())
                assert decoder.eof
            layer_digests[member.name] = uncompressed.hexdigest()
            if member.name.startswith("blobs/sha256/"):
                assert raw.hexdigest() == member.name.rsplit("/", 1)[-1]
    exported = {}
    for row in manifest or []:
        for ref in row.get("RepoTags") or []:
            if ref not in refs:
                continue
            digest, config = docs[row["Config"]]
            layers = ["sha256:" + layer_digests[name] for name in row["Layers"]]
            assert layers == config["rootfs"]["diff_ids"] == refs[ref]["RootFS"]["Layers"]
            assert config["architecture"] == "amd64" and config["os"] == "linux"
            assert refs[ref]["Id"] == inspect(ref)["Id"]
            exported[ref] = dict(
                id=digest,
                tags=[ref],
                engine_image_id=refs[ref]["Id"],
                rootfs_diff_ids=layers,
                architecture=config["architecture"],
                os=config["os"],
                user=config["config"].get("User", ""),
            )
    assert set(exported) == set(refs)
    partial.rename(target)
    with target.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    report = dict(
        passed=True,
        archive=target.name,
        bytes=target.stat().st_size,
        sha256=digest,
        all_layers_independently_rehashed=True,
        images=list(exported.values()),
        verification=verified,
    )
    (args.output / "images.json").write_text(json.dumps(list(exported.values()), indent=2) + "\n")
    (args.output / "image-export.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("images", "verification")}), flush=True)


if __name__ == "__main__":
    main()
