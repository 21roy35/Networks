"""Classify airline emails and extract structured flight data."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from dateutil import parser as dateparser

from .airlines import AIRLINES, airline_for_domain, airline_for_name

# ---------------------------------------------------------------------------
# Email kinds
# ---------------------------------------------------------------------------

BOOKING = "booking"
ETICKET = "eticket"
BOARDING_PASS = "boarding_pass"
CHECKIN = "checkin"
DELAY = "delay"
CANCELLATION = "cancellation"
GATE_CHANGE = "gate_change"
RECEIPT = "receipt"
REFUND = "refund"
SCHEDULE_CHANGE = "schedule_change"

KIND_LABELS = {
    BOOKING: "Booking confirmation",
    ETICKET: "E-ticket",
    BOARDING_PASS: "Boarding pass",
    CHECKIN: "Check-in",
    DELAY: "Delay notice",
    CANCELLATION: "Cancellation notice",
    GATE_CHANGE: "Gate change",
    RECEIPT: "Payment receipt",
    REFUND: "Refund",
    SCHEDULE_CHANGE: "Schedule change",
}

_KIND_PATTERNS = [
    (BOARDING_PASS, r"boarding\s*pass"),
    (CHECKIN, r"check[\s-]*in\s+(?:is\s+)?(?:open|now|complete|confirmed)|checked\s+in|online\s+check-?in"),
    (ETICKET, r"e-?ticket|electronic\s+ticket|ticket\s+(?:number|no\.?|receipt)|ticket\s+issued"),
    (DELAY, r"\bdelay(?:ed)?\b|new\s+departure\s+time|revised\s+departure"),
    (CANCELLATION, r"\bcancell?ed\b|\bcancellation\b"),
    (GATE_CHANGE, r"gate\s+change|new\s+gate"),
    (REFUND, r"\brefund"),
    (SCHEDULE_CHANGE, r"schedule\s+change|itinerary\s+change|time\s+change"),
    (RECEIPT, r"receipt|payment\s+(?:confirmation|received|successful)|invoice"),
    (BOOKING, r"booking\s+(?:confirmation|confirmed|reference)|reservation\s+confirm|your\s+(?:booking|reservation|itinerary|trip)|flight\s+confirmation"),
]

# ---------------------------------------------------------------------------
# Field extraction patterns
# ---------------------------------------------------------------------------

_PNR_RE = re.compile(
    r"(?:booking\s+(?:reference|ref\.?|code|number)|confirmation\s+(?:code|number)"
    r"|reservation\s+(?:code|number|reference)|record\s+locator|\bPNR\b)"
    r"\s*(?:is|:|#|-)?\s*([A-Z0-9]{5,8})\b",
    re.IGNORECASE,
)

_TICKET_RE = re.compile(
    r"(?:e-?ticket|ticket)\s*(?:number|no\.?|#)?\s*(?:is|:|-)?\s*(\d{3}[- ]?\d{10})\b",
    re.IGNORECASE,
)
_TICKET_BARE_RE = re.compile(r"\b(\d{3}-\d{10})\b")

_FLIGHT_RE = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?-?\s?(\d{1,4})\b")

_ROUTE_NAMED_RE = re.compile(
    r"(?:from\s+)?([A-Za-z][A-Za-z .'-]{2,30})\s*\(([A-Z]{3})\)\s*"
    r"(?:to|→|->|–|-)\s*([A-Za-z][A-Za-z .'-]{2,30})\s*\(([A-Z]{3})\)"
)
_ROUTE_CODES_RE = re.compile(r"\b([A-Z]{3})\s*(?:to|→|->|–)\s*([A-Z]{3})\b")

_DEPARTURE_RE = re.compile(
    r"(?:departure|departs?|departing|dep\.?)\s*(?:date|time|at|on)?\s*[:\-]?\s*"
    r"([A-Za-z0-9, :/\-]{6,40}?\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)
_ARRIVAL_RE = re.compile(
    r"(?:arrival|arrives?|arriving|arr\.?|landing)\s*(?:date|time|at|on)?\s*[:\-]?\s*"
    r"([A-Za-z0-9, :/\-]{6,40}?\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?:flight\s+date|travel\s+date|date\s+of\s+travel|date)\s*[:\-]\s*"
    r"([0-9]{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|[A-Za-z]{3,9}\s+[0-9]{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)

_CLASS_RE = re.compile(
    r"(?:cabin|travel\s+class|class(?:\s+of\s+service)?|fare\s+(?:family|type))\s*[:\-]?\s*"
    r"(first|business(?:\s+guest)?|premium\s+economy|economy|guest)\b",
    re.IGNORECASE,
)
_SEAT_RE = re.compile(r"\bseat\s*(?:number|no\.?)?\s*[:\-]?\s*(\d{1,2}[A-K])\b", re.IGNORECASE)
_GATE_RE = re.compile(r"\bgate\s*[:\-]?\s*([A-Z]?\d{1,3}[A-Z]?)\b", re.IGNORECASE)
_BOARDING_TIME_RE = re.compile(
    r"boarding\s*(?:time|starts?|begins?)?\s*[:\-]?\s*(\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)
_PASSENGER_RE = re.compile(
    r"(?i:passenger|traveller|traveler|guest)\s*(?i:name)?\s*[:\-]\s*"
    r"([A-Z][A-Za-z]+(?:[ /][A-Z][A-Za-z.]+){1,4})"
)
_PAYMENT_RE = re.compile(
    r"\b(visa|mastercard|master\s*card|mada|american\s+express|amex|apple\s+pay|"
    r"stc\s+pay|paypal|tabby|tamara)\b(?:\s*(?:card|credit\s+card|debit\s+card))?"
    r"(?:[^\n]{0,30}?(?:ending(?:\s+in)?|\*{2,}|x{2,})\s*(\d{4}))?",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(?:total(?:\s+(?:amount|paid|price|fare))?|amount\s+(?:paid|charged)|grand\s+total|fare)"
    r"\s*[:\-]?\s*(SAR|USD|EUR|GBP|AED|QAR|KWD|BHD|OMR|EGP|TRY|JOD)?\s*"
    r"([\d,]+(?:\.\d{1,2})?)\s*(SAR|USD|EUR|GBP|AED|QAR|KWD|BHD|OMR|EGP|TRY|JOD)?",
    re.IGNORECASE,
)
_DELAY_HOURS_RE = re.compile(
    r"delayed\s+(?:by\s+)?(?:approximately\s+|about\s+)?(\d+(?:\.\d+)?)\s*hours?",
    re.IGNORECASE,
)
_NEW_DEPARTURE_RE = re.compile(
    r"(?:new|revised|updated)\s+departure\s*(?:time)?\s*[:\-]?\s*"
    r"([A-Za-z0-9, :/\-]{4,40}?\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)
_NEW_ARRIVAL_RE = re.compile(
    r"(?:new|revised|updated|estimated|expected)\s+arrival\s*(?:time)?\s*[:\-]?\s*"
    r"([A-Za-z0-9, :/\-]{4,40}?\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)

_AIRLINE_CODES = set(AIRLINES.keys())

# Words that look like flight codes but are not (avoid "NO 12", "TO 5", etc.)
_FLIGHT_CODE_STOPWORDS = {"NO", "TO", "AT", "ON", "IN", "OF", "PM", "AM", "ID"}


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    sender: str
    sender_domain: str
    date: datetime | None
    kinds: list[str] = field(default_factory=list)
    airline_code: str | None = None
    airline_name: str | None = None
    pnr: str | None = None
    ticket_numbers: list[str] = field(default_factory=list)
    flight_numbers: list[str] = field(default_factory=list)
    origin: str | None = None
    origin_city: str | None = None
    destination: str | None = None
    destination_city: str | None = None
    departure: str | None = None
    arrival: str | None = None
    flight_date: str | None = None
    cabin_class: str | None = None
    seat: str | None = None
    gate: str | None = None
    boarding_time: str | None = None
    passenger: str | None = None
    payment_method: str | None = None
    amount: str | None = None
    currency: str | None = None
    delay_hours: float | None = None
    new_departure: str | None = None
    new_arrival: str | None = None
    body_text: str = ""


def _try_parse_dt(text: str, default: datetime | None = None) -> str | None:
    """Parse a free-text date/time into ISO format, or None."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip(" ,-:")
    try:
        dt = dateparser.parse(cleaned, fuzzy=True, dayfirst=True, default=default)
        return dt.isoformat(sep=" ", timespec="minutes") if dt else None
    except (ValueError, OverflowError):
        return None


def classify(subject: str, body: str) -> list[str]:
    """Return the kinds of airline email this is (may be several)."""
    haystack = f"{subject}\n{body[:4000]}"
    kinds = [kind for kind, pat in _KIND_PATTERNS
             if re.search(pat, haystack, re.IGNORECASE)]
    # An email that mentions a PNR but matched nothing else is still
    # useful as a generic booking-related message.
    if not kinds and _PNR_RE.search(haystack):
        kinds = [BOOKING]
    return kinds


def _extract_flights(text: str) -> list[str]:
    flights = []
    for code, number in _FLIGHT_RE.findall(text):
        code = code.upper()
        if code in _FLIGHT_CODE_STOPWORDS or code not in _AIRLINE_CODES:
            continue
        flight = f"{code}{int(number)}"
        if flight not in flights:
            flights.append(flight)
    return flights


def parse_email(message_id: str, subject: str, sender: str, date: datetime | None,
                body: str) -> ParsedEmail | None:
    """Parse one email. Returns None when it is clearly not flight-related."""
    sender_domain = sender.split("@")[-1].strip("> ").lower() if "@" in sender else ""
    airline_code, airline = airline_for_domain(sender_domain)
    if not airline_code:
        airline_code, airline = airline_for_name(f"{subject}\n{body[:2000]}")

    kinds = classify(subject, body)
    text = f"{subject}\n{body}"
    flights = _extract_flights(text)

    if not airline_code and flights:
        airline_code = flights[0][:2]
        airline = AIRLINES.get(airline_code)

    # Not an airline email at all -> skip.
    if not airline_code and not kinds:
        return None
    if not kinds and not flights and not _PNR_RE.search(text):
        return None

    parsed = ParsedEmail(
        message_id=message_id,
        subject=subject,
        sender=sender,
        sender_domain=sender_domain,
        date=date,
        kinds=kinds,
        airline_code=airline_code,
        airline_name=airline["name"] if airline else None,
        flight_numbers=flights,
        body_text=body,
    )

    if m := _PNR_RE.search(text):
        candidate = m.group(1).upper()
        # PNRs are alphanumeric; reject pure long digit runs (phone bits etc.)
        if not candidate.isdigit() or len(candidate) == 6:
            parsed.pnr = candidate

    tickets = [t.replace(" ", "-") for t in _TICKET_RE.findall(text)]
    tickets += [t for t in _TICKET_BARE_RE.findall(text) if t not in tickets]
    parsed.ticket_numbers = list(dict.fromkeys(
        t if "-" in t else f"{t[:3]}-{t[3:]}" for t in tickets))

    if m := _ROUTE_NAMED_RE.search(text):
        parsed.origin_city = m.group(1).strip()
        parsed.origin = m.group(2)
        parsed.destination_city = m.group(3).strip()
        parsed.destination = m.group(4)
    elif m := _ROUTE_CODES_RE.search(text):
        parsed.origin, parsed.destination = m.group(1), m.group(2)

    base_date = date.replace(tzinfo=None) if date else None
    if m := _DATE_RE.search(text):
        parsed.flight_date = (_try_parse_dt(m.group(1)) or "")[:10] or None
    if m := _DEPARTURE_RE.search(text):
        parsed.departure = _try_parse_dt(m.group(1), default=base_date)
    if m := _ARRIVAL_RE.search(text):
        parsed.arrival = _try_parse_dt(m.group(1), default=base_date)
    if parsed.departure and not parsed.flight_date:
        parsed.flight_date = parsed.departure.split(" ")[0]

    if m := _CLASS_RE.search(text):
        parsed.cabin_class = m.group(1).title()
    if m := _SEAT_RE.search(text):
        parsed.seat = m.group(1).upper()
    if m := _GATE_RE.search(text):
        parsed.gate = m.group(1).upper()
    if m := _BOARDING_TIME_RE.search(text):
        parsed.boarding_time = m.group(1)
    if m := _PASSENGER_RE.search(text):
        parsed.passenger = m.group(1).strip()

    if m := _PAYMENT_RE.search(text):
        method = re.sub(r"\s+", " ", m.group(1)).title()
        method = {"Amex": "American Express", "Mada": "MADA", "Stc Pay": "STC Pay"}.get(method, method)
        parsed.payment_method = method + (f" •••• {m.group(2)}" if m.group(2) else "")
    if m := _AMOUNT_RE.search(text):
        parsed.currency = (m.group(1) or m.group(3) or "").upper() or None
        parsed.amount = m.group(2)

    if m := _DELAY_HOURS_RE.search(text):
        parsed.delay_hours = float(m.group(1))
    if m := _NEW_DEPARTURE_RE.search(text):
        parsed.new_departure = _try_parse_dt(m.group(1), default=base_date)
    if m := _NEW_ARRIVAL_RE.search(text):
        parsed.new_arrival = _try_parse_dt(m.group(1), default=base_date)

    return parsed
