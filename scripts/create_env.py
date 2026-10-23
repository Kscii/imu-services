"""Create a private environment using verified release images; never overwrite secrets."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets


def validated_images(manifest, versions):
    if not isinstance(manifest, dict) or manifest.get("schema") != "imu.deployment.v1":
        raise ValueError("Unsupported deployment manifest")
    for name in ("service_version", "library_version"):
        if manifest.get(name) != versions[name]:
            raise ValueError(f"Deployment {name} does not match this release")
    images = manifest.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("Deployment images must be an object")
    for target in ("runtime", "query"):
        ref = images.get(target, "")
        pattern = rf"ghcr\.io/kscii/imu-services-{target}@sha256:[0-9a-f]{{64}}"
        if not isinstance(ref, str) or not re.fullmatch(pattern, ref):
            raise ValueError(f"Expected an immutable {target} image in the release registry")
    return {target: images[target] for target in ("runtime", "query")}


def create_environment(root, mode="online", manifest_path=None):
    versions = json.loads((root / "release-versions.json").read_text())
    if mode == "online":
        path = manifest_path or root / "deployment.json"
        if not path.is_file():
            raise ValueError("Missing deployment.json: download the online deployment bundle, "
                             "or use --mode offline with verified local images")
        images = validated_images(json.loads(path.read_text()), versions)
    elif mode == "offline":
        images = {name: f"localhost/imu-{name}:{versions['service_version']}"
                  for name in ("runtime", "query")}
    else:
        raise ValueError("Unsupported installation mode")
    replacements = {"IMU_RUNTIME_IMAGE": images["runtime"], "IMU_QUERY_IMAGE": images["query"]}
    for name in ("IMU_ADMIN_DB_PASSWORD", "IMU_RUNTIME_DB_PASSWORD", "IMU_QUERY_DB_PASSWORD",
                 "IMU_WRITE_TOKEN", "IMU_READ_TOKEN"):
        replacements[name] = secrets.token_hex(32)
    lines, seen = [], set()
    for line in (root / ".env.example").read_text().splitlines():
        key = line.partition("=")[0]
        if key in replacements:
            if key in seen:
                raise ValueError(f"Duplicate environment template key: {key}")
            seen.add(key)
            line = key + "=" + replacements[key]
        lines.append(line)
    if seen != replacements.keys():
        raise ValueError("Incomplete environment template")
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("online", "offline"), default="online")
    parser.add_argument("--manifest", type=Path, help="Online release deployment.json")
    args = parser.parse_args()
    try:
        create_environment(Path(__file__).resolve().parents[1], args.mode, args.manifest)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Environment not created: {exc}\n")
    print(f"Created .env (0600) for {args.mode} installation; credentials are not printed.")


if __name__ == "__main__":
    main()
