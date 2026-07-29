"""Clone one verified GACA payload without altering its submitted complaint."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db, gaca_normal_browser


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job_id")
    parser.add_argument("--expected-passenger", required=True)
    args = parser.parse_args()

    source = db.get_portal_job(args.source_job_id)
    if (not source
            or str(source.get("kind") or "").casefold() != "gaca"
            or not source.get("terminal")
            or not source.get("reference")):
        raise SystemExit(
            "The source must be a completed GACA job with a reference.")
    payload = copy.deepcopy(source.get("payload") or {})
    passenger = str(payload.get("passenger_name") or "").strip()
    if passenger.casefold() != args.expected_passenger.strip().casefold():
        raise SystemExit(f"Passenger mismatch: {passenger!r}.")

    for key in list(payload):
        if key.startswith("_gaca_") or key in {
            "portal_complaint_id", "portal_auto_key", "portal_inflight_key",
        }:
            payload.pop(key, None)
    job_id = uuid.uuid4().hex
    db.save_portal_job({
        "id": job_id,
        "kind": "gaca",
        "airline_code": source.get("airline_code") or "SV",
        "flight_number": source.get("flight_number") or "",
        "flight_key": source.get("flight_key") or "",
        "complaint_id": None,
        "status": "retry_wait",
        "message": (
            "Controlled duplicate diagnostic of verified GACA job "
            f"{args.source_job_id}; the original complaint is unchanged."
        ),
        "reference": "",
        "terminal": False,
        "attempts": 0,
        "max_attempts": 100000,
        "next_attempt_at": time.time() + 60,
        "payload": payload,
    })
    if not gaca_normal_browser.rotate_proxy_session():
        raise SystemExit("The GACA proxy session file is not configured.")
    db.clear_gaca_portal_circuit()
    print(json.dumps({
        "job_id": job_id,
        "source_job_id": args.source_job_id,
        "source_reference": source.get("reference"),
        "passenger": passenger,
        "flight_number": source.get("flight_number"),
        "due_in_seconds": 60,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
