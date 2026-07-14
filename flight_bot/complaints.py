"""Build structured complaint payloads for official web portals."""

from __future__ import annotations

from datetime import date

from .airlines import AIRLINES, GACA
from .compensation import ELIGIBLE, POSSIBLY, assess, effective


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


def _subject(flight: dict, prefix: str) -> str:
    return (f"{prefix} – flight {effective(flight, 'flight_number') or 'N/A'} "
            f"on {effective(flight, 'flight_date') or 'unknown date'} – "
            f"PNR {flight.get('pnr') or 'N/A'}")


def airline_complaint(flight: dict, user: dict, incident: str = "") -> dict:
    """Generate the airline claim shown to the user and sent to its portal."""
    assessment = assess(flight)
    code = flight.get("airline_code")
    info = AIRLINES.get(code, {})
    airline = flight.get("airline_name") or code or "the airline"
    incident = incident.strip() or "Describe what went wrong."
    subject = _subject(flight, "Passenger rights complaint")
    body = f"""Dear {airline} Customer Relations,

I am filing a formal complaint about the following journey:

{_flight_facts(flight, user)}

What went wrong:
  {incident}

Requested resolution:
  Please apply every refund, compensation, reimbursement and duty-of-care
  remedy available under the applicable passenger-rights rules, and provide a
  written decision with a complaint reference number.

Assessment basis:
  {"; ".join(assessment["frameworks"])}.

Yours faithfully,
{user.get('full_name') or effective(flight, 'passenger') or '[Passenger]'}
{user.get('email') or ''}
{user.get('phone') or ''}
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
                   airline_complaint_date: str = "") -> dict:
    """Generate the regulator escalation sent through GACA's official portal."""
    assessment = assess(flight)
    airline = flight.get("airline_name") or flight.get("airline_code") or "the airline"
    incident = incident.strip() or "Describe what went wrong."
    subject = _subject(flight, f"GACA escalation against {airline}")
    body = f"""Formal escalation to {GACA['name']}

Airline complaint reference: {airline_reference or '[required]'}
Airline complaint date: {airline_complaint_date or '[required]'}

Flight details:
{_flight_facts(flight, user)}

What went wrong and why escalation is required:
  {incident}

Requested resolution:
  Please investigate this complaint and require the carrier to provide every
  remedy due under the applicable passenger-rights rules.

Assessment basis:
  {"; ".join(assessment["frameworks"])}.
"""
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
                      ai_analysis: dict | None = None) -> dict:
    """Return normalized fields consumed by all official-site adapters."""
    incident = " ".join((incident or "").split()).strip()
    if len(incident) < 15:
        raise ValueError("Describe what went wrong in at least 15 characters.")
    if kind not in {"airline", "gaca"}:
        raise ValueError("Unsupported complaint destination.")
    if kind == "gaca" and not airline_reference:
        raise ValueError("Submit to the airline first so GACA receives its reference number.")

    trip_passenger = effective(flight, "passenger") or ""
    profile_name = user.get("full_name") or ""
    explicit_names = [
        str(user.get("first_name") or "").strip(),
        str(user.get("middle_name") or "").strip(),
        str(user.get("last_name") or "").strip(),
    ]
    passenger = (" ".join(filter(None, explicit_names))
                 if explicit_names[0] or explicit_names[2]
                 else profile_name or trip_passenger)
    parsed_first, parsed_middle, parsed_last = _names(
        profile_name or trip_passenger)
    first = explicit_names[0] or parsed_first
    middle = explicit_names[1] or parsed_middle
    last = explicit_names[2] or parsed_last
    origin = effective(flight, "origin") or ""
    destination = effective(flight, "destination") or ""
    ticket_numbers = flight.get("ticket_numbers") or []
    assessment = assess(flight)
    complaint_incident = incident
    if ai_analysis:
        facts = [str(item).strip() for item in ai_analysis.get("facts") or []
                 if str(item).strip()]
        sections = [f"Passenger's original statement: {incident}"]
        if ai_analysis.get("summary"):
            sections.append("Organized issue summary: "
                            + str(ai_analysis["summary"]).strip())
        if facts:
            sections.append("Facts stated by the passenger: " + "; ".join(facts))
        observations = [
            str(item).strip()
            for item in ai_analysis.get("evidence_observations") or []
            if str(item).strip()
        ]
        if observations:
            sections.append("Visible evidence observations: "
                            + "; ".join(observations))
        if ai_analysis.get("requested_remedy"):
            sections.append("Requested resolution: "
                            + str(ai_analysis["requested_remedy"]).strip())
        complaint_incident = "\n  ".join(sections)
    letter = (gaca_complaint(
        flight, user, complaint_incident, airline_reference, airline_complaint_date)
        if kind == "gaca" else airline_complaint(
            flight, user, complaint_incident))
    departure = effective(flight, "departure") or ""
    return {
        "kind": kind,
        "airline_code": flight.get("airline_code") or "",
        "airline_name": flight.get("airline_name")
                        or flight.get("airline_code") or "",
        "passenger_name": passenger,
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "national_id": user.get("national_id") or "",
        "title": user.get("title") or "",
        "nationality": user.get("nationality") or "",
        "country_code": user.get("country_code") or "",
        "alfursan_id": user.get("alfursan_id") or "",
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
    if payload.get("airline_code") == "SV" and payload["kind"] == "airline":
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
    return [label for field, label in required.items() if not payload.get(field)]
