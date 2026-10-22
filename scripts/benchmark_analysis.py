"""Bounded synthetic analysis HTTP benchmark; no model accuracy or real person data."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import time
from uuid import UUID

from fastapi.testclient import TestClient
from imu_services import query
from imu_services.replay import write_json
from imu_services.settings import Settings


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--segments", type=int, default=100000)
    p.add_argument("--days", type=int, default=31)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    if not 1 <= args.segments <= 100000 or not 1 <= args.days <= 31:
        p.error("benchmark must stay within the public API limits")

    class Store:
        def ready(self):
            return True

    class Reader:
        def __init__(self, store):
            pass

        def run(self, key):
            return dict(status="completed", recording_id="synthetic-limit", device_id="synthetic",
                        start_time="2026-10-01T00:00:00Z", sealed_end=args.segments, fingerprint="fixture",
                        provenance={}, summary={"finished": True, "steps": {
                            "enabled": True, "estimated_step_count": args.segments,
                            "analyzed_s": args.segments/25, "unresolved_valid_s": 0}})

        def results(self, key, after=0, limit=100):
            if after:
                return []
            timeline = [dict(start=i, end=i+1, label="sitting" if i % 2 else "standing", status="observed")
                        for i in range(args.segments)]
            steps = [dict(kind="step_segment", start=i, end=i+1, analyzed=True, event_samples=[i])
                     for i in range(args.segments)]
            return [dict(id=1, run_id=str(key), final=True, content_sha256="0"*64,
                         events=[dict(kind="activity_core", start=0, end=args.segments, timeline=timeline), *steps])]

    query.PostgresReader = Reader
    settings = Settings("unused", "synthetic-analysis-test-token")
    with TestClient(query.create_app(settings, Store())) as client:
        start = time.monotonic()
        response = client.post("/v2/analysis/activity", headers={"Authorization": "Bearer "+settings.api_token},
                               json=dict(participant_id="synthetic", run_ids=[str(UUID(int=1))], timezone="UTC",
                                         date_from="2026-10-01",
                                         date_to=(date(2026, 10, 1)+timedelta(days=args.days-1)).isoformat()))
        response.raise_for_status()
        elapsed = time.monotonic()-start
        result = response.json()
    assert sum(day["observed_s"] for day in result["calendar"]["daily"]) == args.segments/25
    assert result["calendar"]["steps"]["estimated_step_count"] == args.segments
    assert sum(day["steps"]["estimated_step_count"] or 0 for day in result["calendar"]["daily"]) == args.segments
    memory = {line.split(":", 1)[0]: int(line.split()[1])
              for line in Path("/proc/self/status").read_text().splitlines()
              if line.startswith(("VmHWM:", "VmRSS:"))}
    report = dict(scope="synthetic maximum-bound HTTP analysis; in-process, excludes database and network",
                  segments=args.segments, step_segments=args.segments, step_events=args.segments,
                  dates=args.days, wall_s=elapsed,
                  peak_rss_mib=memory["VmHWM"]/1024, final_rss_mib=memory["VmRSS"]/1024,
                  memory_measurement="Linux /proc/self/status VmHWM and VmRSS",
                  response_bytes=len(response.content), status="PASS")
    write_json(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
