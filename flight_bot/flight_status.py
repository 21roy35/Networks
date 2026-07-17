"""Persistent, source-attributed flight status and disruption evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import db
from .airlines import AIRLINES
from .compensation import effective


FINAL_STATUSES = {"landed", "cancelled", "diverted"}
ACTIVE_STATUSES = {"boarding", "departed", "airborne", "delayed"}
_IATA_TO_ICAO = {
    "RUH": "OERK", "JED": "OEJN", "DMM": "OEDF", "MED": "OEMA",
    "AHB": "OEAB", "TIF": "OETF", "GIZ": "OEGN", "ELQ": "OEGS",
    "DXB": "OMDB", "AUH": "OMAA", "DOH": "OTHH", "BAH": "OBBI",
    "KWI": "OKKK", "CAI": "HECA", "AMM": "OJAI", "IST": "LTFM",
    "LHR": "EGLL", "CDG": "LFPG", "FRA": "EDDF", "JFK": "KJFK",
}


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_flight_time(value) -> datetime | None:
    """Parse an itinerary wall time for comparisons with stored local times."""
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


def _parse_instant(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _flight_number(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _callsign_candidates(flight: dict) -> list[str]:
    marketing = _flight_number(effective(flight, "flight_number"))
    if not marketing:
        return []
    code = str(flight.get("airline_code") or "").upper()
    icao = str((AIRLINES.get(code) or {}).get("icao") or "").upper()
    suffix = marketing[len(code):] if code and marketing.startswith(code) else ""
    if not re.fullmatch(r"\d+[A-Z]?", suffix):
        match = re.fullmatch(r"[A-Z]{2,3}(\d+[A-Z]?)", marketing)
        suffix = match.group(1) if match else ""
    candidates = ([icao + suffix] if icao and suffix else []) + [marketing]
    return list(dict.fromkeys(candidates))


def _flight_day(flight: dict) -> date | None:
    text = str(effective(flight, "flight_date") or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _active_window(flight: dict, now: datetime | None = None) -> bool:
    """Limit paid/live calls to a small window around the actual itinerary."""
    wall_now = (now or datetime.now()).replace(tzinfo=None)
    departure = parse_flight_time(effective(flight, "departure"))
    arrival = (parse_flight_time(effective(flight, "actual_arrival"))
               or parse_flight_time(flight.get("new_arrival"))
               or parse_flight_time(effective(flight, "arrival")))
    if departure or arrival:
        start = (departure or arrival) - timedelta(hours=6)
        end = (arrival or departure) + timedelta(hours=4)
        return start <= wall_now <= end
    day = _flight_day(flight)
    return bool(day and abs((wall_now.date() - day).days) <= 1)


def poll_interval_seconds(flight: dict, snapshot: dict | None = None,
                          now: datetime | None = None) -> int:
    """Adaptive polling: sparse before departure, frequent only while active."""
    wall_now = (now or datetime.now()).replace(tzinfo=None)
    if (snapshot or {}).get("status") in FINAL_STATUSES:
        return 6 * 60 * 60
    departure = parse_flight_time(effective(flight, "departure"))
    arrival = (parse_flight_time(flight.get("new_arrival"))
               or parse_flight_time(effective(flight, "arrival")))
    if departure:
        until_departure = departure - wall_now
        if until_departure > timedelta(hours=6):
            return 6 * 60 * 60
        if until_departure > timedelta(minutes=90):
            return 30 * 60
        if until_departure > timedelta(0):
            return 10 * 60
    if arrival and wall_now <= arrival + timedelta(hours=3):
        return 4 * 60
    return 60 * 60


def flightaware_status(flight: dict, api_key: str,
                       session=requests) -> dict | None:
    """Return a sufficiently matched AeroAPI record, never a weak best guess."""
    ident = _flight_number(effective(flight, "flight_number"))
    day = _flight_day(flight)
    if not ident or not day:
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
    departure = parse_flight_time(effective(flight, "departure"))

    scored = []
    for record in records:
        record_origin = str((record.get("origin") or {}).get("code_iata") or "").upper()
        record_destination = str(
            (record.get("destination") or {}).get("code_iata") or "").upper()
        # An exact stored route is stronger than a nearby UTC calendar date.
        if origin and record_origin and record_origin != origin:
            continue
        if destination and record_destination and record_destination != destination:
            continue
        score = (2 if origin and record_origin == origin else 0)
        score += (2 if destination and record_destination == destination else 0)
        scheduled_text = str(record.get("scheduled_out")
                             or record.get("scheduled_off") or "")
        scheduled = _parse_instant(scheduled_text)
        if scheduled and scheduled.date() == day:
            score += 3
        elif scheduled and abs((scheduled.date() - day).days) <= 1:
            score += 1
        distance = math.inf
        if departure and scheduled:
            distance = abs((scheduled.replace(tzinfo=None) - departure).total_seconds())
        scored.append((score, -distance, record))
    if not scored:
        return None
    score, _distance, result = max(scored, key=lambda item: (item[0], item[1]))
    required = 4 if origin and destination else 3
    return result if score >= required else None


def airplanes_live_status(flight: dict, session=requests,
                          base_url: str = "https://api.airplanes.live/v2") -> dict | None:
    """Return the freshest exact callsign match from an account-free ADS-B feed."""
    candidates = _callsign_candidates(flight)
    if not candidates:
        return None
    for ident in candidates:
        response = session.get(f"{base_url}/callsign/{ident}", timeout=15,
                               headers={"User-Agent": "FlightDeck/1.0"})
        response.raise_for_status()
        exact = [item for item in (response.json().get("ac") or [])
                 if _flight_number(item.get("flight")) == ident]
        if exact:
            return min(exact, key=lambda item: float(item.get("seen") or 999999))
    return None


def aviation_weather(airport: str, session=requests) -> dict | None:
    """Fetch one current METAR as context, not as proof of disruption cause."""
    icao = _IATA_TO_ICAO.get(str(airport or "").upper())
    if not icao:
        return None
    response = session.get(
        "https://aviationweather.gov/api/data/metar",
        params={"ids": icao, "format": "json", "hours": 2},
        headers={"User-Agent": "FlightDeck/1.0"}, timeout=15)
    response.raise_for_status()
    records = response.json() or []
    return records[0] if records else None


def _status_from_flightaware(record: dict) -> str:
    text = str(record.get("status") or "").casefold()
    if record.get("actual_in") or record.get("actual_on") or any(
            word in text for word in ("arrived", "landed")):
        return "landed"
    if "cancel" in text:
        return "cancelled"
    if "divert" in text:
        return "diverted"
    if record.get("actual_off") or record.get("actual_out"):
        return "airborne" if not record.get("actual_on") else "landed"
    if "delay" in text:
        return "delayed"
    return "scheduled"


def _status_from_adsb(record: dict) -> str:
    altitude = record.get("alt_baro")
    speed = record.get("gs")
    try:
        airborne = str(altitude).casefold() != "ground" and float(altitude) > 500
    except (TypeError, ValueError):
        airborne = False
    try:
        airborne = airborne or float(speed) > 60
    except (TypeError, ValueError):
        pass
    return "airborne" if airborne else "ground"


def _observation(flight: dict, provider: str, status: str, confidence: float,
                 data: dict, *, provider_flight_id: str = "",
                 source_timestamp: str = "", now: datetime | None = None) -> dict:
    observed_at = _utc_now(now).isoformat()
    stable = json.dumps({"status": status, "source_timestamp": source_timestamp,
                         "data": data}, default=str, sort_keys=True)
    return {
        "flight_key": flight.get("flight_key") or str(flight.get("id") or ""),
        "provider": provider,
        "provider_flight_id": provider_flight_id,
        "status": status,
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "observed_at": observed_at,
        "source_timestamp": source_timestamp or None,
        "raw_hash": hashlib.sha256(stable.encode("utf-8")).hexdigest(),
        "data": data,
    }


def _schedule_observation(flight: dict, now: datetime | None = None) -> dict:
    wall_now = (now or datetime.now()).replace(tzinfo=None)
    departure = parse_flight_time(effective(flight, "departure"))
    arrival = (parse_flight_time(flight.get("new_arrival"))
               or parse_flight_time(effective(flight, "arrival")))
    status = "scheduled"
    if departure and wall_now >= departure:
        status = "scheduled_complete" if arrival and wall_now >= arrival else "due"
    return _observation(flight, "schedule", status, .35, {
        "scheduled_departure": effective(flight, "departure"),
        "scheduled_arrival": effective(flight, "arrival"),
    }, now=now)


def _email_observation(flight: dict, now: datetime | None = None) -> dict | None:
    if not bool(effective(flight, "cancelled")):
        return None
    return _observation(flight, "booking_email", "cancelled", .98, {
        "reason": flight.get("cancellation_reason") or "airline notification",
        "flight_number": effective(flight, "flight_number"),
        "flight_date": effective(flight, "flight_date"),
    }, now=now)


def _flightaware_observation(flight: dict, record: dict,
                             now: datetime | None = None) -> dict:
    fields = {key: record.get(key) for key in (
        "ident", "fa_flight_id", "status", "scheduled_out", "estimated_out",
        "actual_out", "scheduled_off", "estimated_off", "actual_off",
        "scheduled_on", "estimated_on", "actual_on", "scheduled_in",
        "estimated_in", "actual_in", "route", "registration",
    ) if record.get(key) is not None}
    fields["origin"] = (record.get("origin") or {}).get("code_iata")
    fields["destination"] = (record.get("destination") or {}).get("code_iata")
    source_timestamp = str(record.get("actual_in") or record.get("actual_on")
                           or record.get("actual_off") or record.get("estimated_out")
                           or record.get("scheduled_out") or "")
    return _observation(
        flight, "flightaware", _status_from_flightaware(record), .95, fields,
        provider_flight_id=str(record.get("fa_flight_id") or record.get("ident") or ""),
        source_timestamp=source_timestamp, now=now)


def _adsb_observation(flight: dict, record: dict, provider: str,
                      now: datetime | None = None) -> dict:
    fields = {key: record.get(key) for key in (
        "hex", "flight", "lat", "lon", "alt_baro", "alt_geom", "gs",
        "track", "baro_rate", "squawk", "category", "type", "r", "seen",
    ) if record.get(key) is not None}
    seen = float(record.get("seen") or 0)
    source_time = (_utc_now(now) - timedelta(seconds=max(0, seen))).isoformat()
    return _observation(
        flight, provider, _status_from_adsb(record), .82, fields,
        provider_flight_id=str(record.get("hex") or record.get("flight") or ""),
        source_timestamp=source_time, now=now)


def fuse_status(flight: dict, observations: list[dict],
                now: datetime | None = None, errors: list[str] | None = None) -> dict:
    """Derive one explainable status while retaining contradictions and provenance."""
    utc_now = _utc_now(now)
    newest: dict[str, dict] = {}
    for item in observations:
        current = newest.get(item["provider"])
        if not current or str(item.get("observed_at")) > str(current.get("observed_at")):
            newest[item["provider"]] = item

    candidates = []
    priorities = {
        ("booking_email", "cancelled"): 100,
        ("flightaware", "landed"): 98,
        ("flightaware", "cancelled"): 98,
        ("flightaware", "diverted"): 97,
        ("flightaware", "airborne"): 92,
        ("flightaware", "delayed"): 85,
        ("airplanes_live", "airborne"): 80,
        ("adsb_lol", "airborne"): 76,
        ("flightaware", "scheduled"): 60,
        ("schedule", "scheduled_complete"): 25,
        ("schedule", "due"): 20,
        ("schedule", "scheduled"): 20,
    }
    for item in newest.values():
        observed = _parse_instant(item.get("observed_at"))
        if (item.get("provider") in {"airplanes_live", "adsb_lol"}
                and observed and utc_now - observed > timedelta(minutes=20)):
            continue
        priority = priorities.get((item.get("provider"), item.get("status")), 0)
        if priority:
            candidates.append((priority, float(item.get("confidence") or 0), item))
    selected = max(candidates, default=(0, 0, None), key=lambda value: value[:2])[2]
    status = selected.get("status") if selected else "unknown"
    confidence = float(selected.get("confidence") or 0) if selected else 0.0
    statuses = {item.get("status") for item in newest.values()}
    contradictions = []
    if "cancelled" in statuses and statuses.intersection({"airborne", "landed"}):
        contradictions.append("Cancellation evidence conflicts with aircraft movement/arrival evidence.")
    if "landed" in statuses and "airborne" in statuses:
        contradictions.append("Sources disagree whether the flight is airborne or landed.")

    data = dict((selected or {}).get("data") or {})
    snapshot = {
        "flight_key": flight.get("flight_key") or "",
        "status": status,
        "label": status.replace("_", " ").title(),
        "confidence": round(confidence, 2),
        "provider": (selected or {}).get("provider") or "none",
        "updated_at": utc_now.isoformat(),
        "source_timestamp": (selected or {}).get("source_timestamp"),
        "scheduled_departure": data.get("scheduled_out") or data.get("scheduled_departure"),
        "estimated_departure": data.get("estimated_out"),
        "actual_departure": data.get("actual_out") or data.get("actual_off"),
        "scheduled_arrival": data.get("scheduled_in") or data.get("scheduled_on")
                             or data.get("scheduled_arrival"),
        "estimated_arrival": data.get("estimated_in") or data.get("estimated_on"),
        "actual_arrival": data.get("actual_in") or data.get("actual_on"),
        "position": ({key: data.get(key) for key in ("lat", "lon", "alt_baro", "gs", "track")
                      if data.get(key) is not None} or None),
        "contradictions": contradictions,
        "errors": list(errors or []),
        "sources": [{
            "provider": item.get("provider"), "status": item.get("status"),
            "confidence": item.get("confidence"), "observed_at": item.get("observed_at"),
            "source_timestamp": item.get("source_timestamp"),
        } for item in sorted(newest.values(),
                             key=lambda value: str(value.get("observed_at")), reverse=True)],
    }
    return snapshot


def _provider_due(observations: list[dict], provider: str, minutes: int,
                  now: datetime | None = None) -> bool:
    latest = next((item for item in observations if item.get("provider") == provider), None)
    observed = _parse_instant((latest or {}).get("observed_at"))
    return not observed or _utc_now(now) - observed >= timedelta(minutes=minutes)


def refresh_flight_status(config: dict, flight: dict, *, session=requests,
                          force: bool = False, now: datetime | None = None,
                          persist: bool = True) -> dict:
    """Refresh available providers, persist evidence, and return the fused snapshot."""
    if now is None:
        timezone_name = str((config.get("telegram") or {}).get(
            "timezone") or "Asia/Riyadh")
        try:
            now = datetime.now(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            now = datetime.now().astimezone()
    db.init_db()
    key = flight.get("flight_key") or str(flight.get("id") or "")
    previous = db.get_flight_status_snapshot(key) if persist else None
    if previous and not force:
        updated = _parse_instant(previous.get("updated_at"))
        if updated and (_utc_now(now) - updated).total_seconds() < poll_interval_seconds(
                flight, previous, now):
            return previous

    settings = config.get("flight_status") or {}
    existing = db.list_flight_status_observations(key, 100) if persist else []
    collected = [_schedule_observation(flight, now)]
    email = _email_observation(flight, now)
    if email:
        collected.append(email)
    errors = []

    if _active_window(flight, now):
        api_key = str(settings.get("flightaware_api_key") or "")
        jobs = {}
        weather = {}
        with ThreadPoolExecutor(max_workers=5,
                                thread_name_prefix="flight-status") as executor:
            if api_key and _provider_due(existing, "flightaware", 4, now):
                jobs[executor.submit(
                    flightaware_status, flight, api_key, session)] = (
                        "flightaware", "")
            if settings.get("airplanes_live_enabled", True) and _provider_due(
                    existing, "airplanes_live", 3, now):
                jobs[executor.submit(
                    airplanes_live_status, flight, session)] = (
                        "airplanes_live", "")
            if settings.get("adsb_lol_enabled", True) and _provider_due(
                    existing, "adsb_lol", 5, now):
                jobs[executor.submit(
                    airplanes_live_status, flight, session,
                    "https://api.adsb.lol/v2")] = ("adsb_lol", "")
            if settings.get("weather_enabled", True) and _provider_due(
                    existing, "aviation_weather", 60, now):
                for airport in dict.fromkeys((effective(flight, "origin"),
                                              effective(flight, "destination"))):
                    airport = str(airport or "").upper()
                    if airport in _IATA_TO_ICAO:
                        jobs[executor.submit(
                            aviation_weather, airport, session)] = (
                                "aviation_weather", airport)

            for future in as_completed(jobs):
                provider, airport = jobs[future]
                try:
                    record = future.result()
                except Exception as exc:
                    labels = {
                        "flightaware": "FlightAware",
                        "airplanes_live": "Airplanes.live",
                        "adsb_lol": "adsb.lol",
                        "aviation_weather": "Aviation weather",
                    }
                    errors.append(
                        f"{labels[provider]} unavailable ({type(exc).__name__})")
                    collected.append(_observation(
                        flight, provider, "unavailable", 0, {
                            "checked_at": _utc_now(now).isoformat(),
                            "error_type": type(exc).__name__,
                            "airport": airport,
                        }, now=now))
                    continue
                if not record:
                    collected.append(_observation(
                        flight, provider, "no_match", 0, {
                            "checked_at": _utc_now(now).isoformat(),
                            "flight_number": effective(flight, "flight_number"),
                            "airport": airport,
                        }, now=now))
                    continue
                if provider == "flightaware":
                    collected.append(_flightaware_observation(flight, record, now))
                elif provider in {"airplanes_live", "adsb_lol"}:
                    collected.append(_adsb_observation(
                        flight, record, provider, now))
                else:
                    weather[airport] = record

        if weather:
            newest_time = max(str(item.get("obsTime") or item.get("reportTime") or "")
                              for item in weather.values())
            collected.append(_observation(
                flight, "aviation_weather", "weather_context", .60,
                weather, source_timestamp=newest_time, now=now))

    if persist:
        for item in collected:
            db.save_flight_status_observation(item)
        observations = db.list_flight_status_observations(key, 100)
    else:
        observations = collected
    snapshot = fuse_status(flight, observations, now, errors)
    if persist:
        db.save_flight_status_snapshot(key, snapshot)
    return snapshot


def get_flight_status(config: dict, flight: dict, *, refresh: bool = False,
                      session=requests, now: datetime | None = None) -> dict:
    key = flight.get("flight_key") or str(flight.get("id") or "")
    if not refresh:
        cached = db.get_flight_status_snapshot(key)
        if cached:
            return cached
    return refresh_flight_status(
        config, flight, session=session, force=refresh, now=now)


def live_landed(config: dict, flight: dict, session=requests) -> bool | None:
    """Backward-compatible survey helper backed by the rich status snapshot."""
    settings = config.get("flight_status") or {}
    key = str(settings.get("flightaware_api_key") or "")
    if settings.get("provider") == "flightaware" and key:
        record = flightaware_status(flight, key, session=session)
        if not record:
            return None
        return _status_from_flightaware(record) == "landed"
    snapshot = get_flight_status(config, flight, session=session)
    if snapshot.get("status") == "landed":
        return True
    if snapshot.get("status") in ACTIVE_STATUSES | {"cancelled", "diverted"}:
        return False
    return None
