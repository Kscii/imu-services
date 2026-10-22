#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
backup_dir="${IMU_BACKUP_DIR:-backups}"
mkdir -p "$backup_dir"
backup_path="$backup_dir/imu-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U imu_admin -d imu -Fc --no-owner > "$backup_path.partial"
mv "$backup_path.partial" "$backup_path"
sha256sum "$backup_path" > "$backup_path.sha256"
# Externally provided SSH destination must be trusted; no automatic host-key acceptance.
if [[ -z "${IMU_BACKUP_DESTINATION:-}" ]]; then
  echo "Local backup created. IMU_BACKUP_DESTINATION is required for an off-host backup." >&2
  exit 2
fi
rsync -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' \
  "$backup_path" "$backup_path.sha256" "$IMU_BACKUP_DESTINATION"
echo "Backup and checksum copied to the configured off-host destination."
