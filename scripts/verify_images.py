"""Verify offline image config digests across classic and containerd image stores."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def verify_saved_export(stream, expected, inspected):
    """Docker 29's .Id can name a manifest; export resolves tag -> exact config bytes."""
    configs, manifest = {}, None
    wanted = {image["id"].removeprefix("sha256:") for image in expected.values()}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile() or member.size > 1024*1024:
                continue
            data = archive.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(data)
            digest = hashlib.sha256(data).hexdigest()
            if digest in wanted:
                configs[member.name] = (digest, json.loads(data))
    if manifest is None:
        raise RuntimeError("image export has no Docker archive manifest")
    found = {}
    for row in manifest:
        for tag in row.get("RepoTags") or []:
            if tag not in expected:
                continue
            if row["Config"] not in configs:
                raise RuntimeError(f"Image config digest mismatch: {tag}")
            digest, config = configs[row["Config"]]
            if digest != expected[tag]["id"].removeprefix("sha256:"):
                raise RuntimeError(f"Image config digest mismatch: {tag}")
            if config["rootfs"]["diff_ids"] != inspected[tag]["RootFS"]["Layers"]:
                raise RuntimeError(f"Image layer identity mismatch: {tag}")
            found[tag] = digest
    if set(found) != set(expected):
        raise RuntimeError("export did not contain every requested image tag")
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--images", type=Path, default=Path(__file__).resolve().parents[1]/"reports/images.json")
    args = p.parse_args()
    expected = {next(tag for tag in image["tags"] if tag.startswith("localhost/imu-")): image
                for image in json.loads(args.images.read_text())}
    inspected = {tag: json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
                 for tag in expected}
    needs_export = any(inspected[tag]["Id"].removeprefix("sha256:") != image["id"].removeprefix("sha256:")
                       for tag, image in expected.items())
    if needs_export:
        process = subprocess.Popen(["docker", "image", "save", *expected], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            verify_saved_export(process.stdout, expected, inspected)
            process.stdout.close()
            error = process.stderr.read().decode()
            if process.wait() != 0:
                raise RuntimeError("docker image save failed: "+error)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
    for tag, image in expected.items():
        actual = inspected[tag]
        if actual["Architecture"] != image["architecture"] or actual["Os"] != image["os"]:
            raise RuntimeError(f"Image platform mismatch: {tag}")
        if actual["Config"].get("User", "") != image.get("user", ""):
            raise RuntimeError(f"Image execution user mismatch: {tag}")
        print(f"Verified exact image config: {tag} ({image['id']})")


if __name__ == "__main__":
    main()
