"""Set one verified GACA job's contact email without changing identity data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--expected-passenger", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()

    email = args.email.strip().casefold()
    if not re.fullmatch(
            r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", email, re.I):
        raise SystemExit("The replacement contact email is invalid.")
    job = db.get_portal_job(args.job_id)
    if not job or str(job.get("kind") or "").casefold() != "gaca":
        raise SystemExit("The selected durable job is not a GACA job.")
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        raise SystemExit("The selected GACA job has no decoded payload.")
    passenger = str(payload.get("passenger_name") or "").strip()
    if passenger.casefold() != args.expected_passenger.strip().casefold():
        raise SystemExit(f"Passenger mismatch: {passenger!r}.")

    previous = str(payload.get("email") or "").strip()
    payload["email"] = email
    job["payload"] = payload
    db.save_portal_job(job)
    print(json.dumps({
        "job_id": args.job_id,
        "passenger": passenger,
        "previous_email": previous,
        "email": email,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
