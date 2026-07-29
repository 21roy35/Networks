"""Build structured complaint payloads for official web portals."""

from __future__ import annotations

from datetime import date

from .airlines import AIRLINES, GACA
from .compensation import ELIGIBLE, POSSIBLY, assess, effective
from .config import passenger_profile_key


def _line(label: str, value) -> str:
    return f"  {label}: {value}\n" if value not in (None, "", []) else ""


def _flight_facts(flight: dict, user: dict) -> str:
    tickets = ", ".join(flight.get("ticket_numbers") or []) or None
    flights = (effective(flight, "flight_number")
               or ", ".join(flight.get("flight_numbers") or []) or None)
    origin = effective(flight, "origin")
    destination = effective(flight, "destination")
    route = f"{origin or '?'} → {destination or '?'}" if origin or destination else None
    facts = ""
    facts += _line("Passenger name", effective(flight, "passenger")
                   or user.get("full_name"))
    facts += _line("Contact email", user.get("email"))
    facts += _line("Contact phone", user.get("phone"))
    facts += _line("Airline", flight.get("airline_name")
                   or flight.get("airline_code"))
    facts += _line("Flight number(s)", flights)
    facts += _line("Flight date", effective(flight, "flight_date"))
    facts += _line("Route", route)
    facts += _line("Booking reference (PNR)", flight.get("pnr"))
    facts += _line("Ticket number(s)", tickets)
    facts += _line("Scheduled departure", effective(flight, "departure"))
    facts += _line("Scheduled arrival", effective(flight, "arrival"))
    facts += _line("Actual/latest arrival", effective(flight, "actual_arrival")
                   or flight.get("new_arrival"))
    facts += _line("Payment method", effective(flight, "payment_method"))
    return facts.rstrip()


def _names(full_name: str) -> tuple[str, str, str]:
    parts = full_name.split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", parts[0]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def _clean_passenger_name(value: str) -> str:
    """Remove parser labels that can trail a passenger's booking name."""
    import re
    value = re.sub(r"\be[\s-]*ticket\b.*$", "", str(value or ""),
                   flags=re.IGNORECASE)
    return " ".join(value.split()).strip(" ,;:-")


def _same_passenger(booking_name: str, profile: dict) -> bool:
    """Match a booking name to the primary user without fuzzy family guesses."""
    booking_key = passenger_profile_key(booking_name)
    first = str(profile.get("first_name") or "").strip()
    middle = str(profile.get("middle_name") or "").strip()
    last = str(profile.get("last_name") or "").strip()
    candidates = [
        profile.get("full_name"),
        " ".join(filter(None, (first, middle, last))),
        # Itinerary emails often omit the middle name used on GACA forms.
        " ".join(filter(None, (first, last))),
    ]
    candidate_keys = {passenger_profile_key(value) for value in candidates if value}
    if booking_key in candidate_keys:
        return True
    # Some itinerary emails expose only the first name.  Accept that exact
    # one-token match, but never use partial/fuzzy surname matching.
    booking_parts = booking_key.split()
    first_parts = passenger_profile_key(first).split()
    return len(booking_parts) == 1 and bool(first_parts) and booking_parts == first_parts[:1]


def _passenger_profile(booking_name: str, profiles: dict) -> dict | None:
    key = passenger_profile_key(booking_name)
    direct = profiles.get(key)
    if isinstance(direct, dict):
        return direct
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        known_name = profile.get("booking_name") or profile.get("full_name") or ""
        if passenger_profile_key(known_name) == key:
            return profile
    return None


def _subject(flight: dict, prefix: str) -> str:
    return (f"{prefix} – flight {effective(flight, 'flight_number') or 'N/A'} "
            f"on {effective(flight, 'flight_date') or 'unknown date'} – "
            f"PNR {flight.get('pnr') or 'N/A'}")


def _sentence(value: str) -> str:
    text = " ".join(str(value or "").split()).strip(" .")
    return text + "." if text else ""


def _lower_first(value: str) -> str:
    if len(value) > 1 and value[0].isupper() and value[1].islower():
        return value[0].lower() + value[1:]
    return value


def _natural_incident(value: str) -> str:
    """Turn analysis prose into the passenger's own concise first-person voice."""
    import re
    text = " ".join(str(value or "").split()).strip(" .")
    text = re.sub(
        r"^(?:additional issue summary|facts stated by the passenger|"
        r"requested resolution)\s*:\s*", "", text, flags=re.I)
    text = re.sub(
        r"^(?:the\s+)?passenger\s+(?:reports?|reported|states?|stated)"
        r"(?:\s+that)?\s+", "", text, flags=re.I)
    if not text:
        return "I am writing to explain an issue I experienced on this trip."
    if re.search(r"\b(?:I|I'm|I've|my|we|our)\b", text, re.I):
        return _sentence(text)
    return _sentence("The issue I experienced was that " + _lower_first(text))


def _natural_resolution(value: str) -> str:
    import re
    text = " ".join(str(value or "").split()).strip(" .")
    text = re.sub(
        r"^(?:the\s+)?passenger\s+(?:requests?|requested|would like)"
        r"(?:\s+that)?\s+", "", text, flags=re.I)
    if not text:
        return "I would appreciate it if you could look into this and provide a fair resolution."
    text = re.sub(r"^investigation\b", "investigate this", text, flags=re.I)
    text = re.sub(
        r"\band (?:an )?appropriate remed(?:y|ies)\b",
        "and provide an appropriate resolution", text, flags=re.I)
    text = re.sub(
        r"\bprovide applicable remedies\b", "provide a fair resolution",
        text, flags=re.I)
    if re.match(r"^(?:I|please)\b", text, re.I):
        return _sentence(text)
    return _sentence(
        "I would appreciate it if you could " + _lower_first(text))


def _gaca_free_text(
        value: str,
        flight: dict,
        user: dict,
        airline_reference: str,
        airline_complaint_date: str) -> str:
    """Remove values already supplied in dedicated GACA form controls."""
    import re

    text = " ".join(str(value or "").split()).strip()
    text = re.sub(
        r"^(?:during|on|regarding)\s+my\s+(?:flight|trip|journey)\b"
        r"[^.!?]{0,220}?,\s*",
        "",
        text,
        flags=re.I,
    )
    exact_values = [
        airline_reference,
        airline_complaint_date,
        user.get("national_id"),
        user.get("full_name"),
        effective(flight, "passenger"),
        effective(flight, "flight_number"),
        effective(flight, "flight_date"),
        flight.get("pnr"),
        *(flight.get("ticket_numbers") or []),
    ]
    for structured in exact_values:
        structured = str(structured or "").strip()
        if structured:
            text = re.sub(re.escape(structured), "", text, flags=re.I)
    # Ghala may naturally spell an ISO form date as "5 July 2026". Remove
    # those human-readable equivalents as well so the narrative does not
    # repeat a value already supplied in GACA's Flight Date control.
    try:
        from datetime import datetime

        travel_date = datetime.strptime(
            str(effective(flight, "flight_date") or ""), "%Y-%m-%d")
        day = travel_date.day
        month = travel_date.strftime("%B")
        year = travel_date.year
        natural_dates = (
            rf"\b0?{day}(?:st|nd|rd|th)?\s+{month}\s+{year}\b",
            rf"\b{month}\s+0?{day}(?:st|nd|rd|th)?[,]?\s+{year}\b",
            rf"\b0?{travel_date.month}[/-]0?{day}[/-]{year}\b",
            rf"\b0?{day}[/-]0?{travel_date.month}[/-]{year}\b",
        )
        for pattern in natural_dates:
            text = re.sub(pattern, "", text, flags=re.I)
    except (TypeError, ValueError):
        pass
    origin_values = [
        effective(flight, "origin"), flight.get("origin_city"),
    ]
    destination_values = [
        effective(flight, "destination"), flight.get("destination_city"),
    ]
    for origin in filter(None, origin_values):
        for destination in filter(None, destination_values):
            text = re.sub(
                rf"\bfrom\s+{re.escape(str(origin))}\s+to\s+"
                rf"{re.escape(str(destination))}\b",
                "",
                text,
                flags=re.I,
            )
    airline = str(
        flight.get("airline_name") or flight.get("airline_code") or ""
    ).strip()
    if airline:
        text = re.sub(re.escape(airline), "the airline", text, flags=re.I)
    text = re.sub(
        r"\b(?:PNR|booking reference|ticket number|national ID|"
        r"airline complaint reference|complaint reference)\s*[:#-]?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:on|dated)\s*(?=[,.;:!?]|$)", "", text, flags=re.I)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"(?:\s*,\s*){2,}", ", ", text)
    return text.strip(" ,.;:")


def _journey(flight: dict, airline: str) -> str:
    number = effective(flight, "flight_number") or "the flight"
    travel_date = effective(flight, "flight_date") or "the travel date"
    origin = effective(flight, "origin") or ""
    destination = effective(flight, "destination") or ""
    route = f" from {origin} to {destination}" if origin and destination else ""
    pnr = flight.get("pnr") or ""
    booking = f", booking reference {pnr}" if pnr else ""
    return f"my {airline} flight {number}{route} on {travel_date}{booking}"


def _requested_resolution(assessment: dict, requested: str = "") -> str:
    requested = " ".join(str(requested or "").split()).strip()
    if requested:
        return requested
    return "look into this and provide a fair resolution"


def airline_complaint(flight: dict, user: dict, incident: str = "",
                      requested_remedy: str = "") -> dict:
    """Generate the airline claim shown to the user and sent to its portal."""
    assessment = assess(flight)
    code = flight.get("airline_code")
    info = AIRLINES.get(code, {})
    airline = flight.get("airline_name") or code or "the airline"
    incident = _natural_incident(incident)
    requested_remedy = _requested_resolution(assessment, requested_remedy)
    subject = _subject(flight, "Passenger rights complaint")
    tickets = ", ".join(flight.get("ticket_numbers") or [])
    ticket_line = f"\n\nFor reference, my ticket number is {tickets}.\n" if tickets else ""
    name = user.get("full_name") or effective(flight, "passenger") or "Passenger"
    body = f"""Hello,

I am writing about {_journey(flight, airline)}.

{incident}

{_natural_resolution(requested_remedy)} Please send me a written response and the complaint reference number so I can follow up.{ticket_line}
Thank you,
{name}
"""
    return {
        "airline": airline,
        "portal_url": info.get("complaint_url", ""),
        "subject": subject,
        "body": body,
        "assessment": assessment,
    }


def gaca_complaint(flight: dict, user: dict, incident: str = "",
                   airline_reference: str = "",
                   airline_complaint_date: str = "",
                   requested_remedy: str = "") -> dict:
    """Generate the regulator escalation sent through GACA's official portal."""
    assessment = assess(flight)
    airline = flight.get("airline_name") or flight.get("airline_code") or "the airline"
    incident = _natural_incident(_gaca_free_text(
        incident,
        flight,
        user,
        airline_reference,
        airline_complaint_date,
    ))
    requested_remedy = _requested_resolution(assessment, requested_remedy)
    subject = _subject(flight, f"GACA escalation against {airline}")
    # GACA receives the passenger, carrier, journey, airline reference, and
    # filing date through dedicated controls. Keep its free-text field focused
    # on the unresolved incident and requested remedy.
    body = (
        f"{incident}\n\n"
        f"{_natural_resolution(requested_remedy)} "
        f"I would appreciate GACA's help obtaining a written decision and "
        f"a proper resolution from the airline."
    )
    return {
        "airline": airline,
        "portal_url": GACA["portal_url"],
        "subject": subject,
        "body": body,
        "assessment": assessment,
    }


def complaint_payload(flight: dict, user: dict, kind: str, incident: str,
                      airline_reference: str = "",
                      airline_complaint_date: str = "",
                      attachments: list[str] | None = None,
                      ai_analysis: dict | None = None,
                      passenger_profiles: dict | None = None) -> dict:
    """Return normalized fields consumed by all official-site adapters."""
    incident = " ".join((incident or "").split()).strip()
    if len(incident) < 15:
        raise ValueError("Describe what went wrong in at least 15 characters.")
    if kind not in {"airline", "gaca"}:
        raise ValueError("Unsupported complaint destination.")
    if kind == "gaca" and not airline_reference:
        raise ValueError("Submit to the airline first so GACA receives its reference number.")

    trip_passenger = _clean_passenger_name(effective(flight, "passenger") or "")
    profiles = passenger_profiles if isinstance(passenger_profiles, dict) else {}
    is_primary = not trip_passenger or _same_passenger(trip_passenger, user)
    selected_profile = (user if is_primary
                        else _passenger_profile(trip_passenger, profiles))
    passenger_profile_missing = bool(trip_passenger and not is_primary
                                     and selected_profile is None)
    identity = selected_profile or {}
    profile_name = identity.get("full_name") or trip_passenger
    explicit_names = [
        str(identity.get("first_name") or "").strip(),
        str(identity.get("middle_name") or "").strip(),
        str(identity.get("last_name") or "").strip(),
    ]
    passenger = (" ".join(filter(None, explicit_names))
                 if explicit_names[0] or explicit_names[2]
                 else trip_passenger or profile_name)
    parsed_first, parsed_middle, parsed_last = _names(
        profile_name or trip_passenger)
    has_explicit_names = bool(explicit_names[0] or explicit_names[2])
    first = explicit_names[0] or parsed_first
    # An explicitly structured profile may intentionally have no middle name.
    # Do not recreate one by splitting a multiword family name from full_name.
    middle = explicit_names[1] if has_explicit_names else parsed_middle
    last = explicit_names[2] or parsed_last
    origin = effective(flight, "origin") or ""
    destination = effective(flight, "destination") or ""
    ticket_numbers = flight.get("ticket_numbers") or []
    assessment = assess(flight)
    requested_remedy = str((ai_analysis or {}).get("requested_remedy") or "")
    # Ghala's analysis guides category/remedy selection, but the portal text
    # stays a short first-person account instead of an internal case report.
    complaint_incident = str(
        (ai_analysis or {}).get("summary") or incident).strip()
    contact_email = identity.get("email") or user.get("email") or ""
    contact_phone = identity.get("phone") or user.get("phone") or ""
    contact_country_code = (identity.get("country_code")
                            or user.get("country_code") or "")
    letter_user = {
        **identity,
        "full_name": passenger or trip_passenger,
        "email": contact_email,
        "phone": contact_phone,
    }
    letter = (gaca_complaint(
        flight, letter_user, complaint_incident, airline_reference,
        airline_complaint_date, requested_remedy)
        if kind == "gaca" else airline_complaint(
            flight, letter_user, complaint_incident, requested_remedy))
    departure = effective(flight, "departure") or ""
    return {
        "kind": kind,
        "flight_key": flight.get("flight_key") or "",
        "airline_code": flight.get("airline_code") or "",
        "airline_name": flight.get("airline_name")
                        or flight.get("airline_code") or "",
        "passenger_name": passenger,
        "booking_passenger_name": trip_passenger,
        "passenger_is_primary": is_primary,
        "passenger_profile_missing": passenger_profile_missing,
        "profile_passenger_name": trip_passenger if passenger_profile_missing else "",
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "email": contact_email,
        "phone": contact_phone,
        "national_id": identity.get("national_id") or "",
        "title": identity.get("title") or "",
        "gender": (
            str(identity.get("gender") or "").strip()
            or (
                "Male" if str(identity.get("title") or "").strip().casefold()
                in {"mr", "mr.", "mister"} else
                "Female" if str(identity.get("title") or "").strip().casefold()
                in {"mrs", "mrs.", "ms", "ms.", "miss", "miss."} else
                ""
            )
        ),
        "nationality": identity.get("nationality") or "",
        "country_code": contact_country_code,
        "alfursan_id": identity.get("alfursan_id") or "",
        "pnr": flight.get("pnr") or "",
        "ticket_number": ticket_numbers[0] if ticket_numbers else "",
        "flight_number": effective(flight, "flight_number")
                         or ", ".join(flight.get("flight_numbers") or []),
        "flight_date": effective(flight, "flight_date") or "",
        "event_time": departure[11:16] if len(departure) >= 16 else "",
        "origin": origin,
        "destination": destination,
        "route": f"{origin} → {destination}" if origin or destination else "",
        "incident": incident,
        "ai_analysis": dict(ai_analysis or {}),
        "subject": letter["subject"],
        "description": letter["body"],
        "airline_reference": airline_reference,
        "airline_complaint_date": airline_complaint_date,
        "claim_likely": assessment["verdict"] in {ELIGIBLE, POSSIBLY},
        "prepared_on": date.today().isoformat(),
        "attachments": list(attachments or []),
    }


def missing_portal_fields(payload: dict) -> list[str]:
    """List information that cannot be safely guessed for an official form."""
    required = {
        "passenger_name": "passenger name",
        "email": "contact email",
        "phone": "phone number",
        "pnr": "booking reference (PNR)",
        "flight_number": "flight number",
        "flight_date": "flight date",
    }
    missing = []
    if payload.get("passenger_profile_missing"):
        name = payload.get("profile_passenger_name") or "this passenger"
        missing.append(f"saved identity profile for {name}")
    if (payload.get("airline_code") == "SV" and payload["kind"] == "airline"
            and not payload.get("passenger_profile_missing")):
        required["ticket_number"] = "e-ticket number"
        required["title"] = "title"
        required["nationality"] = "nationality"
        required["country_code"] = "phone country code"
        required["national_id"] = "passport, National ID, or Iqama number"
    if payload["kind"] == "gaca":
        required.update({
            "airline_reference": "airline complaint reference",
            "airline_complaint_date": "airline complaint date",
        })
    missing.extend(label for field, label in required.items()
                   if not payload.get(field))
    return missing
