"""Print the live GACA circuit and durable queue without mutating either."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flight_bot import db


def _local_time(value: float | int | None) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(float(value)).astimezone().isoformat()


def main() -> None:
    circuit_until = db.gaca_portal_circuit_until()
    jobs = []
    for job in db.list_portal_jobs(limit=500):
        if str(job.get("kind") or "").casefold() != "gaca":
            continue
        if job.get("terminal"):
            continue
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        jobs.append({
            "id": job.get("id"),
            "complaint_id": job.get("complaint_id"),
            "flight_number": job.get("flight_number"),
            "passenger_name": payload.get("passenger_name"),
            "incident_preview": " ".join(
                str(payload.get("incident") or "").split())[:160],
            "status": job.get("status"),
            "attempts": job.get("attempts"),
            "next_attempt_at": _local_time(job.get("next_attempt_at")),
            "reference": job.get("reference"),
            "message_preview": " ".join(
                str(job.get("message") or "").split())[:240],
            "last_error_preview": " ".join(
                str(job.get("last_error") or "").split())[:240],
            "screenshot_file": job.get("screenshot_file"),
        })
    print(json.dumps({
        "now": _local_time(time.time()),
        "circuit_until": _local_time(circuit_until),
        "due_count": len(db.list_due_portal_jobs(20)),
        "active_gaca_jobs": jobs,
        "recent_telegram_updates": [{
            "created_at": item.get("created_at"),
            "media_kind": item.get("media_kind"),
            "text_preview": " ".join(
                str(item.get("text") or "").split())[:180],
        } for item in db.list_telegram_messages(30)
          if item.get("direction") == "outgoing"][-8:],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
