"""Package a small deployment bundle from tagged source and pushed image digests."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from create_env import validated_images


def registry_manifest(path, versions, commit):
    images = {}
    for ref in path.read_text().splitlines():
        for target in ("runtime", "query"):
            if ref.startswith(f"ghcr.io/kscii/imu-services-{target}@"):
                if target in images:
                    raise ValueError(f"Duplicate registry image: {target}")
                images[target] = ref
                break
        else:
            raise ValueError("Unexpected registry image entry")
    manifest = dict(schema="imu.deployment.v1", **versions, source_commit=commit, images=images)
    validated_images(manifest, versions)
    return manifest


def build(root, registry, output):
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root):
        raise ValueError("Freeze reviewed source before packaging")
    versions = json.loads((root / "release-versions.json").read_text())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    epoch = int(subprocess.check_output(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=root))
    manifest = registry_manifest(registry, versions, commit)
    paths = ["compose.yaml", ".env.example", "release-versions.json", "LICENSE", "THIRD_PARTY_NOTICES.md",
             "scripts/create_env.py", "scripts/smoke_http.py", "docs/api.md", "docs/operations.md",
             "docs/architecture.md"]
    paths += [p.relative_to(root).as_posix() for p in sorted((root / "operations").glob("*.sh"))]
    # Read committed bytes: ignored or untracked local files cannot enter a release.
    files = {name: subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root)
             for name in paths}
    files["README.md"] = subprocess.check_output(
        ["git", "show", f"{commit}:docs/deploy-readme.md"], cwd=root)
    files["deployment.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                                for name, data in sorted(files.items())).encode()
    prefix = f"imu-deploy-{versions['service_version']}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (prefix + ".tar.gz")
    checksum = output / (archive.name + ".sha256")
    if archive.exists() or checksum.exists():
        raise FileExistsError("Do not overwrite an existing deployment bundle")
    with archive.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(prefix + "/" + name)
                info.size, info.mtime, info.mode = len(data), epoch, 0o644
                tar.addfile(info, io.BytesIO(data))
    with checksum.open("x") as stream:
        stream.write(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-images", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.registry_images, args.output))


if __name__ == "__main__":
    main()
