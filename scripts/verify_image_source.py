"""Verify that the delivered service images contain the repository's service code."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
source = root/"src/imu_services"
expected = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source.rglob("*")) if p.suffix in (".py", ".sql")}
code = """
import hashlib, json
from pathlib import Path
import imu_services
root=Path(imu_services.__file__).parent
print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
 for p in sorted(root.rglob('*')) if p.suffix in ('.py','.sql')}))
"""
verified = []
for item in json.loads((root/"reports/images.json").read_text()):
    refs = [tag for tag in item["tags"] if tag.startswith(("localhost/imu-runtime:", "localhost/imu-query:"))]
    if not refs:
        continue
    result = subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--read-only",
                                      "--tmpfs", "/tmp", "--entrypoint", "python", refs[0], "-c", code], text=True)
    if json.loads(result) != expected:
        raise SystemExit("Service source mismatch: "+refs[0])
    verified.append({"reference": refs[0], "image_id": item["id"]})
report = {"passed": len(verified) == 2, "images": verified, "service_source_sha256": expected}
if not report["passed"]:
    raise SystemExit("Expected runtime and query images")
(root/"reports/image-source-consistency.json").write_text(json.dumps(report, indent=2)+"\n")
print("Both images contain the exact current service source.")
