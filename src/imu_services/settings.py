from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_token: str = field(repr=False)
    max_batch_samples: int = 15000
    max_body_bytes: int = 4*1024*1024
    max_pending_bytes: int = 512*1024*1024
    max_pending_samples_per_run: int = 90000
    max_pending_batches_per_run: int = 4096
    max_open_runs: int = 1000
    max_whole_pending_bytes: int = 512*1024*1024
    max_whole_runs: int = 4
    max_whole_body_bytes: int = 256*1024*1024
    max_whole_decoded_bytes: int = 512*1024*1024
    max_whole_uploads: int = 2
    upload_timeout_seconds: int = 300
    worker_mode: str = "incremental"
    sign_adaptation: bool = False
    admit_new_runs: bool = True
    lease_seconds: int = 60
    max_failures: int = 5
    processing_quantum: int = 1500
    minimum_ready_samples: int = 250
    threads: int = 2
    steps_enabled: bool = True
    pool_size: int = 8
    poll_seconds: float = .5

    @classmethod
    def from_env(cls, role="api"):
        token_name = "IMU_READ_TOKEN" if role == "query" else "IMU_WRITE_TOKEN"
        token = os.environ.get(token_name, "")
        if role in ("api", "query") and len(token) < 24:
            raise ValueError(f"{token_name} must be supplied and contain at least 24 characters")
        kwargs = {}
        for name in ("max_pending_bytes", "max_pending_samples_per_run", "max_open_runs",
                     "threads", "pool_size", "lease_seconds", "max_whole_pending_bytes",
                     "max_whole_runs", "max_whole_body_bytes", "max_whole_decoded_bytes",
                     "max_whole_uploads", "upload_timeout_seconds"):
            env = "IMU_"+name.upper()
            if env in os.environ:
                kwargs[name] = int(os.environ[env])
                if kwargs[name] < 1:
                    raise ValueError(f"{env} must be positive")
        mode = os.environ.get("IMU_WORKER_MODE", "incremental")
        if mode not in ("incremental", "whole_record"):
            raise ValueError("IMU_WORKER_MODE must be incremental or whole_record")
        return cls(database_url=os.environ["IMU_DATABASE_URL"], api_token=token, worker_mode=mode,
                   sign_adaptation=os.environ.get("IMU_SIGN_ADAPTATION", "false").lower() == "true",
                   admit_new_runs=os.environ.get("IMU_ADMIT_NEW_RUNS", "true").lower() == "true",
                   steps_enabled=os.environ.get("IMU_STEPS_ENABLED", "true").lower() == "true", **kwargs)
