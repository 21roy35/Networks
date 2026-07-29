"""List Mansour's GACA issue groups and verified submission references."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_bot import db


def main() -> None:
    flights = {
        str(item.get("flight_key") or item.get("key") or ""): item
        for item in db.list_flights()
    }
    grouped = defaultdict(list)
    for complaint in db.list_complaints():
        if str(complaint.get("kind") or "").casefold() != "gaca":
            continue
        flight_key = str(complaint.get("flight_key") or "")
        flight = flights.get(flight_key) or {}
        passenger = str(flight.get("passenger") or "")
        if not re.search(r"\bmansour\b", passenger, re.I):
            continue
        text = " ".join(str(
            complaint.get("original_body")
            or complaint.get("submitted_text")
            or complaint.get("body")
            or "").split())
        grouped[flight_key].append({
            "complaint_id": complaint.get("id"),
            "status": complaint.get("status"),
            "reference": complaint.get("reference"),
            "text_preview": text[:300],
        })

    result = []
    for flight_key, complaints in grouped.items():
        flight = flights.get(flight_key) or {}
        result.append({
            "flight_key": flight_key,
            "flight_number": flight.get("flight_number"),
            "flight_date": flight.get("flight_date"),
            "route": flight.get("route"),
            "passenger": flight.get("passenger"),
            "has_verified_gaca_reference": any(
                re.fullmatch(r"C\d{6,}", str(item.get("reference") or ""),
                             re.I)
                for item in complaints
            ),
            "complaints": complaints[:12],
        })
    result.sort(key=lambda item: (
        str(item.get("flight_date") or ""),
        str(item.get("flight_number") or ""),
    ), reverse=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
