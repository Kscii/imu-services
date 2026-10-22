#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Restore only into a new empty database. Existing production databases are never dropped.
backup_path="${1:?provide a verified custom-format pg_dump file}"
restore_db="${2:?provide a NEW restore database name beginning imu_restore_}"
[[ "$restore_db" =~ ^imu_restore_[a-zA-Z0-9_]+$ ]] || { echo "Invalid restore database name" >&2; exit 1; }
docker compose exec -T postgres createdb -U imu_admin "$restore_db"
docker compose exec -T postgres pg_restore -U imu_admin -d "$restore_db" \
  --no-owner --no-privileges --exit-on-error --single-transaction < "$backup_path"
echo "Restored into $restore_db. Validate and reapply roles before selecting it for service traffic."
