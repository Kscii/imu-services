"""Verify current images against every library file and service source file."""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="1.1.1")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    backend = json.loads(subprocess.check_output(["docker", "version", "--format", "{{json .Server}}"],
                                                text=True))
    source = root / "src/imu_services"
    expected = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.suffix in (".py", ".sql")
    }
    wheel = root / "vendor/wheels/imu_inference-1.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as z:
        lib = {
            n.removeprefix("imu_inference/"): hashlib.sha256(z.read(n)).hexdigest()
            for n in z.namelist()
            if n.startswith("imu_inference/") and not n.endswith("/")
        }
        licenses = {n.split('/licenses/', 1)[1]: hashlib.sha256(z.read(n)).hexdigest()
                    for n in z.namelist() if '/licenses/' in n and not n.endswith('/')}
        assert 'LICENSES/CC-BY-SA-4.0.txt' in licenses and 'MODEL_LICENSES.md' in licenses
    code = """
import hashlib,json,importlib.util,importlib.metadata
from pathlib import Path
import imu_services,imu_inference
def files(m,suffixes=None):
 r=Path(m.__file__).parent
 return {p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in r.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and (suffixes is None or p.suffix in suffixes)}
d=importlib.metadata.distribution('imu-inference')
licenses={str(p).split('/licenses/',1)[1]:hashlib.sha256(d.locate_file(p).read_bytes()).hexdigest() for p in d.files if '/licenses/' in str(p)}
print(json.dumps(dict(service=files(imu_services,('.py','.sql')),library=files(imu_inference),licenses=licenses,torch_present=importlib.util.find_spec('torch') is not None,service_version=importlib.metadata.version('imu-services'),library_version=importlib.metadata.version('imu-inference'))))
"""
    images = []
    for target in ("runtime", "query"):
        ref = f"localhost/imu-{target}:{args.tag}"
        info = json.loads(subprocess.check_output(["docker", "image", "inspect", ref], text=True))[0]
        actual = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp",
                    "--entrypoint",
                    "python",
                    ref,
                    "-c",
                    code,
                ],
                text=True,
            )
        )
        assert actual["service"] == expected and actual["library"] == lib
        assert actual["licenses"] == licenses
        assert actual["service_version"] == "1.1.1" and actual["library_version"] == "1.0.1"
        assert actual["torch_present"] == (target == "runtime") and info["Config"]["User"] == "10001:10001"
        assert info["Config"].get("Labels", {}).get("org.opencontainers.image.revision") == revision
        images.append(
            dict(
                reference=ref,
                engine_image_id=info["Id"],
                rootfs_diff_ids=info["RootFS"]["Layers"],
                size_bytes=info["Size"],
            )
        )
    report = dict(
        passed=True,
        backend="native_cpu",
        container_engine=backend,
        source_commit=revision,
        service_version="1.1.1",
        library_version="1.0.1",
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        images=images,
        service_files=expected,
        library_inventory_sha256=hashlib.sha256(json.dumps(lib, sort_keys=True).encode()).hexdigest(),
        library_license_files=licenses,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
