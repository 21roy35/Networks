"""Optional live flight-status lookup with a schedule-based fallback."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import requests

from .compensation import effective


def parse_flight_time(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def schedule_has_finished(flight: dict, delay_minutes: int = 20,
                          now: datetime | None = None) -> bool:
    now = now or datetime.now()
    arrival = (parse_flight_time(effective(flight, "actual_arrival"))
               or parse_flight_time(flight.get("new_arrival"))
               or parse_flight_time(effective(flight, "arrival")))
    return bool(arrival and now >= arrival + timedelta(minutes=delay_minutes))


def _iso_utc(day: date, offset_days: int = 0) -> str:
    value = datetime.combine(day + timedelta(days=offset_days), datetime.min.time(),
                             tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def flightaware_status(flight: dict, api_key: str,
                       session=requests) -> dict | None:
    """Return the best matching AeroAPI flight record, or None."""
    ident = str(effective(flight, "flight_number") or "").replace(" ", "")
    day_text = str(effective(flight, "flight_date") or "")[:10]
    if not ident or not day_text:
        return None
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return None
    response = session.get(
        f"https://aeroapi.flightaware.com/aeroapi/flights/{ident}",
        headers={"x-apikey": api_key},
        params={"start": _iso_utc(day, -1), "end": _iso_utc(day, 1)},
        timeout=20,
    )
    response.raise_for_status()
    records = response.json().get("flights") or []
    origin = str(effective(flight, "origin") or "").upper()
    destination = str(effective(flight, "destination") or "").upper()

    def score(record: dict) -> int:
        value = 0
        if (record.get("origin") or {}).get("code_iata", "").upper() == origin:
            value += 2
        if (record.get("destination") or {}).get("code_iata", "").upper() == destination:
            value += 2
        scheduled = str(record.get("scheduled_out") or record.get("scheduled_off") or "")
        if scheduled[:10] == day_text:
            value += 3
        return value

    return max(records, key=score) if records else None


def live_landed(config: dict, flight: dict, session=requests) -> bool | None:
    status_config = config.get("flight_status") or {}
    if status_config.get("provider") != "flightaware":
        return None
    key = status_config.get("flightaware_api_key") or ""
    if not key:
        return None
    record = flightaware_status(flight, key, session=session)
    if not record:
        return None
    status = str(record.get("status") or "").lower()
    return bool(record.get("actual_in") or record.get("actual_on")
                or "arrived" in status or "landed" in status)
