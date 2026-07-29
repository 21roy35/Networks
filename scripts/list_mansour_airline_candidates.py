"""List Mansour's recent Saudia complaints for distinct GACA candidates."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def _flight_date(flight: dict) -> date | None:
    raw = str(flight.get("flight_date") or flight.get("date") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def main() -> None:
    cutoff = date.today() - timedelta(days=60)
    flights = {
        str(item.get("flight_key") or item.get("key") or ""): item
        for item in db.list_flights()
    }
    rows = []
    for complaint in db.list_complaints():
        if str(complaint.get("kind") or "").casefold() != "airline":
            continue
        flight_key = str(complaint.get("flight_key") or "")
        flight = flights.get(flight_key) or {}
        passenger = str(flight.get("passenger") or "")
        flown = _flight_date(flight)
        if (not re.search(r"\bmansour\b", passenger, re.I)
                or not flown or flown < cutoff):
            continue
        text = " ".join(str(
            complaint.get("original_body")
            or complaint.get("submitted_text")
            or complaint.get("body")
            or "").split())
        rows.append({
            "complaint_id": complaint.get("id"),
            "flight_key": flight_key,
            "flight_number": flight.get("flight_number"),
            "flight_date": flown.isoformat(),
            "passenger": passenger,
            "status": complaint.get("status"),
            "reference": complaint.get("reference"),
            "text_preview": text[:420],
        })
    rows.sort(key=lambda item: (
        item["flight_date"], int(item.get("complaint_id") or 0)),
        reverse=True)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
