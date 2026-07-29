"""Activate exactly one verified GACA job after replacing its proxy session."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db, gaca_normal_browser


def _payload(job: dict) -> dict:
    value = job.get("payload") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--expected-passenger", required=True)
    parser.add_argument("--hold-other-passengers", action="store_true")
    args = parser.parse_args()

    selected = db.get_portal_job(args.job_id)
    if not selected or str(selected.get("kind") or "").casefold() != "gaca":
        raise SystemExit("The selected durable job is not an active GACA job.")
    actual_passenger = str(
        _payload(selected).get("passenger_name") or "").strip()
    if actual_passenger.casefold() != args.expected_passenger.strip().casefold():
        raise SystemExit(
            f"Passenger mismatch: selected job belongs to {actual_passenger!r}.")

    held = []
    if args.hold_other_passengers:
        for job in db.list_portal_jobs(limit=500):
            if (str(job.get("kind") or "").casefold() != "gaca"
                    or job.get("terminal")
                    or str(job.get("id")) == args.job_id):
                continue
            passenger = str(_payload(job).get("passenger_name") or "").strip()
            if passenger.casefold() == actual_passenger.casefold():
                continue
            if db.hold_portal_job(
                    str(job["id"]),
                    "Held because only Mansour's GACA complaints are currently "
                    "in scope."):
                held.append(str(job["id"]))

    if not gaca_normal_browser.rotate_proxy_session():
        raise SystemExit("The GACA proxy session file is not configured.")
    db.clear_gaca_portal_circuit()
    due = db.retry_portal_job(
        args.job_id,
        "Fresh Riyadh residential session prepared for one supervised job.",
        fixed_delay=60,
    )
    if due is None:
        raise SystemExit("The selected GACA job could not be re-queued.")
    print(json.dumps({
        "job_id": args.job_id,
        "passenger": actual_passenger,
        "due": datetime.fromtimestamp(due).astimezone().isoformat(),
        "held_other_jobs": held,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
