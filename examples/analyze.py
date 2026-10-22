"""Example company-service caller: explicit completed runs, identity and calendar."""
import argparse
import json
import os

import httpx


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--participant-id", required=True)
    p.add_argument("--run-id", action="append", default=[])
    p.add_argument("--timezone", required=True)
    p.add_argument("--date-from", required=True)
    p.add_argument("--date-to", required=True)
    args = p.parse_args()
    with httpx.Client(base_url=os.environ["IMU_QUERY_URL"], timeout=60,
                      headers={"Authorization": "Bearer "+os.environ["IMU_READ_TOKEN"]}) as client:
        response = client.post("/v2/analysis/activity", json={
            "participant_id": args.participant_id, "run_ids": args.run_id, "timezone": args.timezone,
            "date_from": args.date_from, "date_to": args.date_to,
        })
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
