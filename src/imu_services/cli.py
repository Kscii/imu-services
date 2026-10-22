import argparse
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["api", "query", "worker", "migrate", "install-roles", "worker-health",
                                           "upgrade-check", "release-info"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    if args.command in ("upgrade-check", "release-info"):
        from .release import release_info, check_upgrade
        import json
        print(json.dumps(release_info() if args.command == "release-info"
                         else check_upgrade(os.environ["IMU_DATABASE_URL"],
                                            sign_adaptation=os.environ.get("IMU_SIGN_ADAPTATION", "false").lower()
                                            == "true",
                                            steps_enabled=os.environ.get("IMU_STEPS_ENABLED", "true").lower()
                                            == "true"), sort_keys=True))
    elif args.command == "install-roles":
        from .operations import install_roles
        install_roles(os.environ["IMU_DATABASE_URL"], os.environ["IMU_RUNTIME_DB_PASSWORD"],
                      os.environ["IMU_QUERY_DB_PASSWORD"])
    elif args.command == "worker-health":
        import time
        from pathlib import Path
        path = Path(os.environ.get("IMU_WORKER_HEALTH_FILE", "/tmp/imu-worker-health"))
        if not path.exists() or time.time()-path.stat().st_mtime > 90:
            raise SystemExit(1)
    elif args.command == "migrate":
        from .store import migrate
        migrate(os.environ["IMU_DATABASE_URL"])
    elif args.command == "worker":
        from .worker import main as worker
        worker()
    else:
        import uvicorn
        uvicorn.run(f"imu_services.{args.command}:create_app", factory=True, host=args.host, port=args.port,
                    timeout_graceful_shutdown=30, proxy_headers=False, access_log=False, limit_concurrency=64)


if __name__ == "__main__":
    main()
