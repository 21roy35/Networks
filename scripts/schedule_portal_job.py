"""Schedule one verified durable portal job after a fixed quiet period."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--expected-passenger", required=True)
    parser.add_argument("--delay-seconds", type=int, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    job = db.get_portal_job(args.job_id)
    payload = (job or {}).get("payload") or {}
    passenger = str(
        payload.get("passenger_name") if isinstance(payload, dict) else ""
    ).strip()
    if passenger.casefold() != args.expected_passenger.strip().casefold():
        raise SystemExit(f"Passenger mismatch: {passenger!r}.")
    due = db.retry_portal_job(
        args.job_id,
        args.reason,
        fixed_delay=max(60, min(args.delay_seconds, 6 * 3600)),
    )
    if due is None:
        raise SystemExit("The selected portal job could not be scheduled.")
    print(json.dumps({
        "job_id": args.job_id,
        "passenger": passenger,
        "due": datetime.fromtimestamp(due).astimezone().isoformat(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
