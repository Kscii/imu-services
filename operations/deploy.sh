#!/usr/bin/env bash
# Invoked by the dispatch workflow from a release directory on the configured host.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
runtime_image="${1:?runtime digest reference}"
query_image="${2:?query digest reference}"
revision="${3:?40-character source commit SHA}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 2
for ref in "$runtime_image" "$query_image"; do
  [[ "$ref" =~ ^ghcr\.io/[a-z0-9/_-]+@sha256:[a-f0-9]{64}$ ]] || exit 2
done
[[ -f .env ]] || { echo "Provision .env through the host secret manager before first deployment" >&2; exit 2; }
docker pull "$runtime_image"
docker pull "$query_image"
for ref in "$runtime_image" "$query_image"; do
  image_revision="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$ref")"
  [[ "$image_revision" == "$revision" ]] || { echo "Image/source revision mismatch" >&2; exit 2; }
done
cp .env .env.previous
old_schema="$(docker compose exec -T postgres psql -U imu_admin -d imu -Atc 'SELECT max(version) FROM schema_migrations' 2>/dev/null || true)"
restore_previous() {
  docker compose stop api worker worker-whole query || true
  current_schema="$(docker compose exec -T postgres psql -U imu_admin -d imu -Atc 'SELECT max(version) FROM schema_migrations' 2>/dev/null || true)"
  if [[ "$current_schema" != "$old_schema" ]]; then
    echo "Schema changed; services remain stopped. Restore the predeploy database backup and .env.previous together before rollback." >&2
    return
  fi
  cp .env.previous .env
  docker compose up -d api worker worker-whole query || true
}
trap restore_previous ERR
# Freeze admission before checking unfinished versions. Committed checkpoints remain durable.
docker compose stop api worker worker-whole query
python3 - "$runtime_image" "$query_image" <<'PY'
import pathlib, sys
p=pathlib.Path(".env")
lines=p.read_text().splitlines()
updates=dict(zip(("IMU_RUNTIME_IMAGE","IMU_QUERY_IMAGE"),sys.argv[1:],strict=True))
updates["IMU_ADMIT_NEW_RUNS"]="false"
lines=[line for line in lines if line.split("=",1)[0] not in updates]
p.write_text("\n".join(lines+[f"{k}={v}" for k,v in updates.items()])+"\n")
PY
docker compose up -d --wait postgres
mkdir -p backups
backup="backups/predeploy-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U imu_admin -d imu -Fc --no-owner > "$backup.partial"
mv "$backup.partial" "$backup"
sha256sum "$backup" > "$backup.sha256"
docker compose run --rm --no-deps api upgrade-check
docker compose run --rm --no-deps migrate
docker compose up -d --wait --wait-timeout 240 api worker worker-whole query
docker compose exec -T api imu-service release-info > release-installed.json
trap - ERR
echo "Selected images are healthy; new-task admission remains paused. Backup and release manifest retained. Resume admission after the release checks."
