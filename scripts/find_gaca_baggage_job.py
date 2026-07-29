"""Read-only audit of baggage complaints and their GACA durable jobs."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def _decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def main() -> None:
    flights = {
        str(item.get("flight_key") or item.get("key") or ""): item
        for item in db.list_flights()
    }
    jobs_by_complaint = {}
    for job in db.list_portal_jobs(limit=500):
        jobs_by_complaint.setdefault(str(job.get("complaint_id")), []).append(job)

    matches = []
    for complaint in db.list_complaints():
        searchable = "\n".join(
            str(complaint.get(key) or "")
            for key in (
                "body", "original_body", "submitted_text",
                "issue_summary", "response_summary",
            )
        )
        if not re.search(r"\bbaggage\b|أمتعة|عفش", searchable, re.I):
            continue
        flight_key = str(complaint.get("flight_key") or "")
        flight = flights.get(flight_key) or {}
        jobs = jobs_by_complaint.get(str(complaint.get("id")), [])
        matches.append({
            "complaint_id": complaint.get("id"),
            "kind": complaint.get("kind"),
            "status": complaint.get("status"),
            "reference": complaint.get("reference"),
            "flight_key": flight_key,
            "flight_number": flight.get("flight_number"),
            "flight_date": flight.get("flight_date"),
            "passenger": flight.get("passenger"),
            "text_preview": " ".join(searchable.split())[:500],
            "jobs": [{
                "id": job.get("id"),
                "kind": job.get("kind"),
                "status": job.get("status"),
                "terminal": job.get("terminal"),
                "reference": job.get("reference"),
                "incident": " ".join(
                    str(_decoded(job.get("payload")).get("incident") or "")
                    .split())[:240],
            } for job in jobs],
        })
    print(json.dumps(matches, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
