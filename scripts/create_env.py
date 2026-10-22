"""Create a private local environment file; never prints generated credentials."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
contents = (root/".env.example").read_text()
for name in ("IMU_ADMIN_DB_PASSWORD", "IMU_RUNTIME_DB_PASSWORD", "IMU_QUERY_DB_PASSWORD",
             "IMU_WRITE_TOKEN", "IMU_READ_TOKEN"):
    contents = contents.replace(f"{name}=\n", f"{name}={secrets.token_hex(32)}\n")
fd = os.open(root/".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(contents)
print("Created .env with mode 0600. Select image digests before production deployment.")
