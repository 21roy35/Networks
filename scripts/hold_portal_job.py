"""Safely hold one verified durable portal job."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--expected-passenger", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    job = db.get_portal_job(args.job_id)
    payload = (job or {}).get("payload") or {}
    passenger = str(
        payload.get("passenger_name") if isinstance(payload, dict) else ""
    ).strip()
    if passenger.casefold() != args.expected_passenger.strip().casefold():
        raise SystemExit(f"Passenger mismatch: {passenger!r}.")
    if not db.hold_portal_job(args.job_id, args.reason):
        raise SystemExit("The selected job could not be held.")
    print(f"Held {args.job_id} for {passenger}.")


if __name__ == "__main__":
    main()
