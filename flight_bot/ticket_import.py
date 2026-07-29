"""Source-grounded ticket imports received through Telegram.

The Telegram coordinator owns the conversational flow.  This module keeps the
extraction, normalization, preview, and synthetic source-record construction
deterministic and independently testable.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser
from pypdf import PdfReader

from .config import passenger_profile_key
from .parser import ETICKET, ParsedEmail, parse_email


_FLIGHT_NUMBER_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{2})[\s-]?(\d{1,4})(?![A-Z0-9])",
    re.IGNORECASE)
_COMPLAINT_CONTEXT_RE = re.compile(
    r"\b(?:complaint|case|claim|service\s+ticket|reference|ref(?:erence)?\.?)\b",
    re.IGNORECASE,
)
_COMPLAINT_REFERENCE_RE = re.compile(
    r"(?<![A-Z0-9])("
    r"C[\s_-]*\d{6,10}|"
    r"[A-Z]{2,8}[-_]\d{5,14}|"
    r"\d{6,10}"
    r")(?!\d)",
    re.IGNORECASE,
)
_DATE_TOKEN_RE = re.compile(
    r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2}[-/]20\d{2}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|"
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2})\b",
    re.IGNORECASE,
)


def empty_ticket_details() -> dict:
    return {
        "airline_code": "",
        "airline_name": "",
        "pnr": "",
        "ticket_numbers": [],
        "passenger": "",
        "cabin_class": "",
        "seat": "",
        "payment_method": "",
        "national_id": "",
        "alfursan_id": "",
        "segments": [],
        "complaint": {
            "reference": "",
            "filed_at": "",
            "text": "",
            "category": "",
            "flight_number": "",
        },
    }


def extract_pdf_text(path: Path, *, max_chars: int = 60_000) -> str:
    """Read embedded PDF text without OCR or executing document content."""
    reader = PdfReader(str(path))
    chunks: list[str] = []
    length = 0
    for page in reader.pages[:30]:
        value = str(page.extract_text() or "").strip()
        if not value:
            continue
        remaining = max_chars - length
        if remaining <= 0:
            break
        chunks.append(value[:remaining])
        length += len(chunks[-1])
    return "\n\n".join(chunks)


def _iso_date(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    try:
        if re.fullmatch(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text):
            parsed = dateparser.parse(text, fuzzy=False, yearfirst=True)
        else:
            parsed = dateparser.parse(text, fuzzy=False, dayfirst=True)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not parsed or parsed.year < 2000 or parsed.year > 2100:
        return ""
    return parsed.date().isoformat()


def _iso_datetime(value: object, flight_date: str = "") -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{1,2}:\d{2}(?:\s*[AP]M)?", text, re.IGNORECASE):
        text = f"{flight_date} {text}" if flight_date else text
    try:
        parsed = dateparser.parse(
            text,
            fuzzy=False,
            dayfirst=not bool(re.match(r"^20\d{2}[-/]", text)),
            yearfirst=bool(re.match(r"^20\d{2}[-/]", text)),
        )
    except (TypeError, ValueError, OverflowError):
        return ""
    if not parsed:
        return ""
    if not flight_date and parsed.year == datetime.now().year:
        # A bare time should not silently acquire today's date.
        return parsed.strftime("%H:%M")
    return parsed.isoformat(sep=" ", timespec="minutes")


def _flight_number(value: object) -> str:
    compact = re.sub(r"[\s-]+", "", str(value or "")).upper()
    match = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4})", compact)
    if not match or not re.search(r"[A-Z]", match.group(1)):
        return ""
    return "".join(match.groups())


def _airport(value: object) -> str:
    code = re.sub(r"[^A-Za-z]", "", str(value or "")).upper()
    return code if len(code) == 3 else ""


def _pnr(value: object) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return compact if 5 <= len(compact) <= 8 else ""


def _ticket_number(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return f"{digits[:3]}-{digits[3:]}" if len(digits) == 13 else ""


def normalize_complaint_reference(value: object) -> str:
    compact = re.sub(r"[\s-]+", "_", str(value or "").strip()).upper()
    if re.fullmatch(r"C_?\d{6,10}", compact):
        return "C_" + re.sub(r"\D", "", compact)
    compact = re.sub(r"[^A-Z0-9_-]", "", compact)
    if (6 <= len(compact) <= 24
            and not (compact.isdigit() and len(compact) == 13)):
        return compact
    return ""


def complaint_reference_from_text(value: str) -> str:
    """Extract a case number only when nearby words identify it as a case."""
    value = str(value or "")
    command = re.search(
        r"(?:^|\s)/complaintref\s+([A-Z0-9_-]{6,24})\b",
        value, re.IGNORECASE)
    if command:
        return normalize_complaint_reference(command.group(1))
    for context in _COMPLAINT_CONTEXT_RE.finditer(value):
        window = value[max(0, context.start() - 30):context.end() + 100]
        match = _COMPLAINT_REFERENCE_RE.search(window)
        if match:
            return normalize_complaint_reference(match.group(1))
    return ""


def manual_complaint_from_text(value: str) -> dict:
    """Extract explicitly labelled manual-case metadata from a message."""
    text = str(value or "")
    filed_at = ""
    filed_match = re.search(
        r"\b(?:filed|submitted|made|raised|complaint\s+date)\b"
        r".{0,30}?"
        r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
        r"\d{1,2}[-/]\d{1,2}[-/]20\d{2}|"
        r"\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|"
        r"[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2})\b",
        text, re.IGNORECASE)
    if filed_match:
        filed_at = _iso_date(filed_match.group(1))
    issue = ""
    issue_match = re.search(
        r"\b(?:about|issue|problem|complaint\s+text|details?)\s*[:=-]?\s*"
        r"(.+)$",
        text, re.IGNORECASE | re.DOTALL)
    if issue_match:
        issue = " ".join(issue_match.group(1).split())[:5000]
    category = ""
    category_match = re.search(
        r"\bcategory\s*[:=-]\s*([^,;\n]{2,100})",
        text, re.IGNORECASE)
    if category_match:
        category = " ".join(category_match.group(1).split())[:120]
    return {
        "reference": complaint_reference_from_text(text),
        "filed_at": filed_at,
        "text": issue,
        "category": category,
        "flight_number": selectors_from_text(text)["flight_number"],
    }


def _segment(value: dict) -> dict:
    date = _iso_date(value.get("flight_date") or value.get("date"))
    departure = _iso_datetime(
        value.get("departure") or value.get("departure_time"), date)
    arrival = _iso_datetime(
        value.get("arrival") or value.get("arrival_time"), date)
    return {
        "flight_number": _flight_number(value.get("flight_number")),
        "flight_date": date,
        "origin": _airport(value.get("origin")),
        "destination": _airport(value.get("destination")),
        "departure": departure,
        "arrival": arrival,
    }


def normalize_ticket_details(value: dict | None) -> dict:
    """Normalize either deterministic or AI-extracted ticket JSON."""
    value = value if isinstance(value, dict) else {}
    output = empty_ticket_details()
    output.update({
        "airline_code": re.sub(
            r"[^A-Za-z0-9]", "", str(value.get("airline_code") or "")
        ).upper()[:3],
        "airline_name": " ".join(
            str(value.get("airline_name") or "").split())[:100],
        "pnr": _pnr(value.get("pnr")),
        "passenger": " ".join(
            str(value.get("passenger") or value.get("passenger_name") or "")
            .split())[:120],
        "cabin_class": " ".join(
            str(value.get("cabin_class") or "").split())[:60],
        "seat": re.sub(
            r"[^A-Za-z0-9]", "", str(value.get("seat") or "")
        ).upper()[:8],
        "payment_method": " ".join(
            str(value.get("payment_method") or "").split())[:100],
        "national_id": re.sub(
            r"[^A-Za-z0-9]", "", str(value.get("national_id") or "")
        )[:20],
        "alfursan_id": re.sub(
            r"\D", "", str(value.get("alfursan_id") or "")
        )[:16],
    })
    raw_tickets = value.get("ticket_numbers") or []
    if isinstance(raw_tickets, str):
        raw_tickets = [raw_tickets]
    output["ticket_numbers"] = list(dict.fromkeys(filter(
        None, (_ticket_number(item) for item in raw_tickets))))
    raw_segments = value.get("segments") or []
    if not raw_segments and any(value.get(key) for key in (
            "flight_number", "flight_date", "origin", "destination",
            "departure", "arrival")):
        raw_segments = [value]
    output["segments"] = []
    for item in raw_segments[:12]:
        if not isinstance(item, dict):
            continue
        normalized = _segment(item)
        if any(normalized.values()):
            output["segments"].append(normalized)
    raw_complaint = value.get("complaint") or {}
    if not isinstance(raw_complaint, dict):
        raw_complaint = {}
    output["complaint"] = {
        "reference": normalize_complaint_reference(
            raw_complaint.get("reference")
            or value.get("complaint_reference")),
        "filed_at": _iso_date(
            raw_complaint.get("filed_at")
            or raw_complaint.get("complaint_date")
            or value.get("complaint_date")),
        "text": " ".join(str(
            raw_complaint.get("text")
            or raw_complaint.get("complaint_text")
            or value.get("complaint_text")
            or "").split())[:5000],
        "category": " ".join(str(
            raw_complaint.get("category")
            or value.get("complaint_category")
            or "").split())[:120],
        "flight_number": _flight_number(
            raw_complaint.get("flight_number")),
    }
    return output


def deterministic_ticket_details(text: str) -> dict:
    """Extract the fields already supported by the normal email parser."""
    text = str(text or "")
    parsed = parse_email(
        "telegram-preview",
        "E-ticket imported from Telegram",
        "telegram@flightdeck.local",
        datetime.now(),
        text,
    )
    if not parsed:
        output = empty_ticket_details()
    else:
        segments = [{
            "flight_number": item.get("flight_number"),
            "flight_date": item.get("date"),
            "origin": item.get("origin"),
            "destination": item.get("destination"),
            "departure": (
                f"{item.get('date')} {item.get('dep_time')}"
                if item.get("date") and item.get("dep_time") else ""),
            "arrival": (
                f"{item.get('date')} {item.get('arr_time')}"
                if item.get("date") and item.get("arr_time") else ""),
        } for item in (parsed.segments or [])]
        if not segments and parsed.flight_numbers:
            segments = [{
                "flight_number": parsed.flight_numbers[0],
                "flight_date": parsed.flight_date,
                "origin": parsed.origin,
                "destination": parsed.destination,
                "departure": parsed.departure,
                "arrival": parsed.arrival,
            }]
        output = normalize_ticket_details({
            "airline_code": parsed.airline_code,
            "airline_name": parsed.airline_name,
            "pnr": parsed.pnr,
            "ticket_numbers": parsed.ticket_numbers,
            "passenger": parsed.passenger,
            "cabin_class": parsed.cabin_class,
            "seat": parsed.seat,
            "payment_method": parsed.payment_method,
            "segments": segments,
        })
        profile = next(iter((parsed.passenger_profiles or {}).values()), {})
        if profile:
            output["national_id"] = str(profile.get("national_id") or "")
            output["alfursan_id"] = str(profile.get("alfursan_id") or "")
    # dateutil's day-first parsing can transpose an otherwise unambiguous
    # ISO date in older parser paths. Explicit Telegram labels win here.
    direct_date = re.search(
        r"\b(?:flight|travel|departure)\s+date\s*[:=-]?\s*"
        r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b",
        text, re.IGNORECASE)
    if direct_date and output.get("segments"):
        flight_date = _iso_date(direct_date.group(1))
        output["segments"][0]["flight_date"] = flight_date
        for field, label in (("departure", "departure"), ("arrival", "arrival")):
            match = re.search(
                rf"\b{label}\s*[:=-]\s*([^\n\r]+)",
                text, re.IGNORECASE)
            if match:
                output["segments"][0][field] = _iso_datetime(
                    match.group(1).strip(), flight_date)
    reference = complaint_reference_from_text(text)
    if reference:
        output["complaint"].update(manual_complaint_from_text(text))
    return output


def merge_ticket_details(primary: dict | None, secondary: dict | None) -> dict:
    """Fill deterministic gaps with grounded AI or follow-up extraction."""
    first = normalize_ticket_details(primary)
    second = normalize_ticket_details(secondary)
    merged = empty_ticket_details()
    for key in (
        "airline_code", "airline_name", "pnr", "passenger", "cabin_class",
        "seat", "payment_method", "national_id", "alfursan_id",
    ):
        merged[key] = first.get(key) or second.get(key) or ""
    merged["ticket_numbers"] = list(dict.fromkeys(
        (first.get("ticket_numbers") or [])
        + (second.get("ticket_numbers") or [])))

    first_segments = list(first.get("segments") or [])
    second_segments = list(second.get("segments") or [])
    used: set[int] = set()
    segments = []
    for base in first_segments:
        match_index = next((
            index for index, candidate in enumerate(second_segments)
            if index not in used and (
                base.get("flight_number")
                and base.get("flight_number") == candidate.get("flight_number")
                or base.get("flight_date")
                and base.get("flight_date") == candidate.get("flight_date")
                and base.get("origin") == candidate.get("origin")
            )
        ), None)
        other = second_segments[match_index] if match_index is not None else {}
        if match_index is not None:
            used.add(match_index)
        segments.append({
            key: base.get(key) or other.get(key) or ""
            for key in (
                "flight_number", "flight_date", "origin", "destination",
                "departure", "arrival",
            )
        })
    segments.extend(
        segment for index, segment in enumerate(second_segments)
        if index not in used)
    merged["segments"] = segments
    merged["complaint"] = {
        key: (first.get("complaint") or {}).get(key)
        or (second.get("complaint") or {}).get(key)
        or ""
        for key in ("reference", "filed_at", "text", "category",
                    "flight_number")
    }
    return normalize_ticket_details(merged)


def missing_ticket_fields(details: dict) -> list[str]:
    details = normalize_ticket_details(details)
    missing = []
    if not details.get("passenger"):
        missing.append("passenger name")
    if not details.get("pnr") and not details.get("ticket_numbers"):
        missing.append("PNR or 13-digit e-ticket number")
    segments = details.get("segments") or []
    if not segments:
        return missing + ["flight number", "flight date", "origin", "destination"]
    for index, segment in enumerate(segments, 1):
        prefix = f"leg {index} " if len(segments) > 1 else ""
        for key, label in (
            ("flight_number", "flight number"),
            ("flight_date", "flight date"),
            ("origin", "origin airport"),
            ("destination", "destination airport"),
        ):
            if not segment.get(key):
                missing.append(prefix + label)
    return missing


def ticket_preview(details: dict) -> str:
    details = normalize_ticket_details(details)
    lines = ["Ticket ready to add:"]
    for index, segment in enumerate(details.get("segments") or [], 1):
        prefix = f"Leg {index}: " if len(details.get("segments") or []) > 1 else ""
        lines.append(
            f"{prefix}{segment.get('flight_number') or '?'} | "
            f"{segment.get('flight_date') or 'date ?'} | "
            f"{segment.get('origin') or '?'} → "
            f"{segment.get('destination') or '?'}"
        )
    if not details.get("segments"):
        lines.append("Flight details not recognized yet")
    lines.extend([
        f"Passenger: {details.get('passenger') or 'missing'}",
        f"PNR: {details.get('pnr') or 'not shown'}",
        "Ticket: " + (
            ", ".join(details.get("ticket_numbers") or []) or "not shown"),
    ])
    if details.get("seat") or details.get("cabin_class"):
        lines.append(
            "Travel: " + " | ".join(filter(None, (
                details.get("cabin_class"), details.get("seat")))))
    if details.get("payment_method"):
        lines.append(f"Payment: {details['payment_method']}")
    complaint = details.get("complaint") or {}
    if complaint.get("reference"):
        lines.append(
            "Manual airline complaint: "
            f"{complaint['reference']} | "
            f"{complaint.get('filed_at') or 'date not supplied'}")
    missing = missing_ticket_fields(details)
    if missing:
        lines.append("Still needed: " + ", ".join(missing))
    return "\n".join(lines)


def _time_only(value: str) -> str | None:
    value = str(value or "")
    match = re.search(r"(?:\s|T)(\d{2}:\d{2})(?::\d{2})?$", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{2}:\d{2}", value):
        return value
    return None


def build_parsed_ticket(
        details: dict,
        *,
        message_id: str,
        imported_at: datetime,
        source_text: str = "",
        source_file: str = "",
) -> ParsedEmail:
    """Create the durable parsed source used by the normal flight linker."""
    details = normalize_ticket_details(details)
    segments = [{
        "origin": segment.get("origin") or None,
        "destination": segment.get("destination") or None,
        "date": segment.get("flight_date") or None,
        "dep_time": _time_only(segment.get("departure") or ""),
        "arr_time": _time_only(segment.get("arrival") or ""),
        "flight_number": segment.get("flight_number") or None,
        "label": None,
    } for segment in details.get("segments") or []]
    normalized_lines = [
        "E-ticket imported from Telegram",
        f"Airline: {details.get('airline_name') or details.get('airline_code')}",
        f"Passenger name: {details.get('passenger')}",
        f"PNR: {details.get('pnr')}",
    ]
    normalized_lines.extend(
        f"Ticket number: {number}"
        for number in details.get("ticket_numbers") or [])
    if details.get("national_id"):
        normalized_lines.append(f"National ID: {details['national_id']}")
    if details.get("alfursan_id"):
        normalized_lines.append(f"Alfursan ID: {details['alfursan_id']}")
    if details.get("payment_method"):
        normalized_lines.append(f"Payment method: {details['payment_method']}")
    for segment in details.get("segments") or []:
        normalized_lines.extend([
            f"Flight number: {segment.get('flight_number')}",
            f"Flight date: {segment.get('flight_date')}",
            f"Route: {segment.get('origin')} to {segment.get('destination')}",
            f"Departure: {segment.get('departure')}",
            f"Arrival: {segment.get('arrival')}",
        ])
    if source_file:
        normalized_lines.append(f"[Attachment: {Path(source_file).name}]")
    body = "\n".join(normalized_lines)
    if source_text:
        body += "\n\nOriginal Telegram source:\n" + source_text[:60_000]
    parsed = parse_email(
        message_id,
        "E-ticket imported from Telegram",
        "telegram@flightdeck.local",
        imported_at,
        body,
    ) or ParsedEmail(
        message_id=message_id,
        subject="E-ticket imported from Telegram",
        sender="telegram@flightdeck.local",
        sender_domain="flightdeck.local",
        date=imported_at,
        kinds=[ETICKET],
        body_text=body,
    )
    parsed.kinds = list(dict.fromkeys([ETICKET] + list(parsed.kinds or [])))
    parsed.airline_code = details.get("airline_code") or parsed.airline_code
    parsed.airline_name = details.get("airline_name") or parsed.airline_name
    parsed.pnr = details.get("pnr") or None
    parsed.ticket_numbers = details.get("ticket_numbers") or []
    parsed.flight_numbers = [
        item["flight_number"] for item in details.get("segments") or []
        if item.get("flight_number")]
    parsed.passenger = details.get("passenger") or None
    parsed.cabin_class = details.get("cabin_class") or None
    parsed.seat = details.get("seat") or None
    parsed.payment_method = details.get("payment_method") or None
    parsed.segments = segments
    if segments:
        first = details["segments"][0]
        parsed.origin = first.get("origin") or None
        parsed.destination = first.get("destination") or None
        parsed.flight_date = first.get("flight_date") or None
        parsed.departure = first.get("departure") or None
        parsed.arrival = first.get("arrival") or None
    passenger = details.get("passenger") or ""
    if passenger and (details.get("national_id") or details.get("alfursan_id")):
        key = passenger_profile_key(passenger)
        profile = dict((parsed.passenger_profiles or {}).get(key) or {})
        profile.update({
            "booking_name": passenger,
            "full_name": passenger,
            "national_id": details.get("national_id") or profile.get("national_id"),
            "alfursan_id": details.get("alfursan_id") or profile.get("alfursan_id"),
        })
        profile["evidence"] = {
            **(profile.get("evidence") or {}),
            **{
                field: "Telegram ticket import"
                for field in ("national_id", "alfursan_id")
                if details.get(field)
            },
        }
        parsed.passenger_profiles = {
            **(parsed.passenger_profiles or {}),
            key: profile,
        }
    parsed.body_text = body
    return parsed


def explicit_ticket_import_request(text: str) -> bool:
    return bool(re.search(
        r"(?:^/ticket\b|"
        r"\b(?:add|import|save|record|upload)\b.{0,50}"
        r"\b(?:ticket|e-?ticket|booking|itinerary|flight)\b)",
        str(text or ""), re.IGNORECASE | re.DOTALL))


def explicit_manual_complaint_request(text: str) -> bool:
    text = str(text or "")
    return bool(re.search(
        r"(?:^/complaintref\b|"
        r"\b(?:add|save|record|attach|filed|submitted|made)\b.{0,70}"
        r"\b(?:complaint|case|claim|reference|ref)\b|"
        r"\b(?:manual|manually)\b.{0,50}\b(?:complaint|case|claim)\b)",
        text, re.IGNORECASE | re.DOTALL))


def selectors_from_text(text: str) -> dict:
    text = str(text or "")
    flight = ""
    for match in _FLIGHT_NUMBER_RE.finditer(text):
        candidate = "".join(match.groups()).upper()
        if re.search(r"[A-Z]", match.group(1)) and not candidate.startswith("C"):
            flight = candidate
            break
    pnr_match = re.search(
        r"\bPNR\s*(?:is|:|#|-)?\s*([A-Z0-9]{5,8})\b",
        text, re.IGNORECASE)
    date_match = _DATE_TOKEN_RE.search(text)
    return {
        "flight_number": flight,
        "pnr": _pnr(pnr_match.group(1)) if pnr_match else "",
        "flight_date": _iso_date(date_match.group(1)) if date_match else "",
    }
