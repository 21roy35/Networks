"""Classify airline emails and extract structured flight data."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from dateutil import parser as dateparser

from .airlines import AIRLINES, airline_for_domain, airline_for_name
from .mail_client import clean_email_body

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

# The delay pattern requires the word "flight" nearby so the boilerplate
# liability text ("... for delay of passengers and baggage") in e-tickets
# does not turn every ticket into a delay notice.
_KIND_PATTERNS = [
    (BOARDING_PASS, r"boarding\s*pass"),
    (CHECKIN, r"check[\s-]*in\s+(?:is\s+)?(?:open|now|complete|confirmed)|checked\s+in|online\s+check-?in|check-?in\s+confirmation"),
    (ETICKET, r"e-?ticket|electronic\s+ticket|ticket\s+(?:number|no\.?|receipt)|ticket\s+issued"),
    (DELAY, r"flight[^.\n]{0,80}\bdelay(?:ed)?\b|\bdelay(?:ed)?\b[^.\n]{0,80}\bflight\b|delayed\s+(?:by|until)|new\s+departure\s+time|revised\s+departure"),
    (CANCELLATION, r"(?:has\s+been|was|is|got)\s+cancell?ed|cancell?ed\s+(?:flight|due|by\s+the\s+airline)"
                   r"|flight\s+cancellation|cancellation\s+(?:notice|notification|of\s+your)"
                   r"|(?:booking|flight|trip|reservation)[^.\n]{0,40}\bcancell?ed\b"),
    (GATE_CHANGE, r"gate\s+change|new\s+gate"),
    (REFUND, r"\brefund"),
    (SCHEDULE_CHANGE, r"re-?scheduled|schedule\s+change|itinerary\s+change|time\s+change"),
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
    r"(?:to|→|->|–|—|-)\s*([A-Za-z][A-Za-z .'-]{2,40})\s*\(([A-Z]{3})\)"
)
_ROUTE_CODES_RE = re.compile(r"\b([A-Z]{3})\s*(?:to|→|->|–|—|>)\s*([A-Z]{3})\b")

# Codes that look like airports but never are (currencies, acronyms).
_NOT_AIRPORTS = {
    "SAR", "USD", "EUR", "GBP", "AED", "QAR", "KWD", "BHD", "OMR", "EGP",
    "TRY", "JOD", "VAT", "GMT", "UTC", "KSA", "UAE", "APP", "FAQ", "PDF",
    "THE", "AND", "FOR", "NEW", "API", "IOS", "SMS", "PIN", "KGS", "MIN",
    "MRS", "TAX", "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "JAN",
    "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
    "DEC",
}

# City names -> IATA, for emails that only say "Riyadh to Abha".
CITY_IATA = {
    "riyadh": "RUH", "jeddah": "JED", "jiddah": "JED", "dammam": "DMM",
    "abha": "AHB", "medina": "MED", "madinah": "MED", "madina": "MED",
    "tabuk": "TUU", "taif": "TIF", "jazan": "GIZ", "gizan": "GIZ",
    "qassim": "ELQ", "gassim": "ELQ", "hail": "HAS", "yanbu": "YNB",
    "najran": "EAM", "bisha": "BHH", "al baha": "ABT", "albaha": "ABT",
    "al jouf": "AJF", "arar": "RAE", "sharurah": "SHW",
    "wadi al dawasir": "DWD", "alula": "ULH", "al ula": "ULH", "neom": "NUM",
    "cairo": "CAI", "alexandria": "HBE", "dubai": "DXB", "abu dhabi": "AUH",
    "sharjah": "SHJ", "doha": "DOH", "bahrain": "BAH", "manama": "BAH",
    "kuwait": "KWI", "muscat": "MCT", "salalah": "SLL", "amman": "AMM",
    "beirut": "BEY", "baghdad": "BGW", "istanbul": "IST", "ankara": "ESB",
    "trabzon": "TZX", "london": "LHR", "manchester": "MAN", "paris": "CDG",
    "frankfurt": "FRA", "munich": "MUC", "amsterdam": "AMS", "madrid": "MAD",
    "rome": "FCO", "milan": "MXP", "vienna": "VIE", "zurich": "ZRH",
    "geneva": "GVA", "athens": "ATH", "new york": "JFK",
    "los angeles": "LAX", "washington": "IAD", "toronto": "YYZ",
    "mumbai": "BOM", "delhi": "DEL", "hyderabad": "HYD", "kochi": "COK",
    "karachi": "KHI", "lahore": "LHE", "islamabad": "ISB", "colombo": "CMB",
    "manila": "MNL", "jakarta": "CGK", "kuala lumpur": "KUL",
    "singapore": "SIN", "bangkok": "BKK", "khartoum": "KRT",
    "casablanca": "CMN", "tunis": "TUN", "algiers": "ALG", "baku": "GYD",
    "tbilisi": "TBS", "sarajevo": "SJJ",
}
_CITY_PAIR_RE = re.compile(
    r"([A-Za-z][A-Za-z ]{2,25}?)\s+(?:to|→|->)\s+([A-Za-z][A-Za-z ]{2,25})",
    re.IGNORECASE,
)

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
# Gulf Air style: the bare time sits on the line after the label.
_DEP_TIME_ONLY_RE = re.compile(
    r"\bdeparture\b\W{0,6}(\d{1,2}:\d{2}(?:\s*[AP]M)?)\b", re.IGNORECASE)
_ARR_TIME_ONLY_RE = re.compile(
    r"\barrival\b\W{0,6}(\d{1,2}:\d{2}(?:\s*[AP]M)?)\b", re.IGNORECASE)
# "Booking date: ..." must NOT become the flight date, so only explicit
# travel-date labels are accepted here.
_DATE_RE = re.compile(
    r"(?:flight\s+date|travel\s+date|date\s+of\s+travel|departure\s+date)\s*[:\-]?\s*"
    r"([0-9]{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|[A-Za-z]{3,9}\s+[0-9]{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)
# "Departing · Tuesday, 16 July [2024]"  /  "· Sun, 21 July"
_DEPARTING_DATE_RE = re.compile(
    r"departing\W{0,8}(?:(?:mon|tues?|wednes|thurs?|fri|satur|sun)day,?\s*)?"
    r"(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+20\d{2})?|[A-Za-z]{3,9}\s+\d{1,2}(?:,?\s+20\d{2})?)",
    re.IGNORECASE,
)
_WEEKDAY_DATE_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+"
    r"(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+20\d{2})?)",
    re.IGNORECASE,
)
# Subject style: "Boarding Open for SV1669 on 16 July" / "on 23 May 2025"
_SUBJECT_ON_DATE_RE = re.compile(
    r"\bon\s+(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+20\d{2})?)", re.IGNORECASE)

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

# Passenger name, in decreasing order of confidence.
_PASSENGER_LABEL_RE = re.compile(
    r"(?i:passenger|traveller|traveler|guest)s?\s*(?i:name)?\s*[:\-]\s*"
    r"([A-Z][A-Za-z]+(?:[ /][A-Z][A-Za-z.]+){1,4})"
)
_PASSENGER_TITLE_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s*([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){1,3})\b"
)
_PASSENGER_DEAR_RE = re.compile(
    r"\b(?:Dear|Hi|Hello)\s+((?:[A-Z][A-Za-z'-]*|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z'-]*|[A-Z]{2,})){0,3})\s*[,\n]"
)
_IDENTITY_PERSON_RE = re.compile(
    r"\b(?P<title>Mr|Mrs|Ms|Miss|Dr)\.?\s*"
    r"(?P<name>[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){1,3})\b"
)
_NOT_NAMES = {
    "guest", "customer", "sir", "madam", "sir/madam", "traveler",
    "traveller", "passenger", "member", "valued customer", "all", "team",
}
# Trailing tokens that are labels truncated onto the name ("... PASSENGER",
# "... MEMBERSHIP"), not part of it.
_NAME_TRAILING_JUNK = re.compile(
    r"\s+(?:pas(?:senger)?s?|memb(?:er(?:ship)?)?|guest|frequent|flyer|"
    r"class|economy|business|adult|seat|e[\s-]*ticket|mr|mrs|ms)$",
    re.IGNORECASE)

_ALFURSAN_ID_RE = re.compile(
    r"(?:frequent\s*flyer|alfursan(?:\s*(?:id|number|membership))?|"
    r"رقم\s*(?:عضوية\s*)?الفرسان)\s*[:#-]?\s*"
    r"(?:alfursan\s*miles\s*[·:\-]?\s*)?(\d{6,12})\b",
    re.IGNORECASE,
)
_NATIONAL_ID_RE = re.compile(
    r"(?P<label>national\s*(?:id|identity)(?:\s*(?:no|number))?|"
    r"gcc\s*/?\s*residence\s*id|iqama(?:\s*(?:no|number))?|"
    r"passport\s*(?:no|number)|"
    r"رقم\s*(?:الهوية(?:\s*الوطنية)?|الإقامة|الجواز))"
    r"\s*[:#-]?\s*(?P<value>(?:\d{10}|[A-Z][A-Z0-9]{5,11}))\b",
    re.IGNORECASE,
)
_NATIONALITY_RE = re.compile(
    r"(?:nationality|الجنسية)\s*[:#-]?\s*"
    r"([A-Za-z][A-Za-z ]{2,30}|[\u0600-\u06ff ]{3,30})",
    re.IGNORECASE,
)
_CONTACT_EMAIL_RE = re.compile(
    r"(?:contact\s*)?e-?mail(?:\s*address)?\s*[:#-]?\s*"
    r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)
_CONTACT_PHONE_RE = re.compile(
    r"(?:mobile|phone|contact\s*(?:number|no))\s*[:#-]?\s*"
    r"(\+?\d[\d ()-]{6,19})",
    re.IGNORECASE,
)

_PNR_STOPWORDS = {"NUMBER", "BOOKING", "TICKET", "FLIGHT", "TRAVEL",
                  "ONLINE", "PLEASE", "BELOW"}

_PAYMENT_RE = re.compile(
    r"\b(visa|mastercard|master\s*card|mada|american\s+express|amex|apple\s+pay|"
    r"stc\s+pay|paypal|tabby|tamara)\b(?:\s*(?:card|credit\s+card|debit\s+card))?",
    re.IGNORECASE,
)
_CARD_LAST4_RE = re.compile(
    r"(?:ending\s*(?:(?:in|with)\s*)?|last\s*(?:four|4)(?:\s*digits)?\s*|"
    r"(?:[•*xX][\s-]*){2,})[:#\s-]*(\d{4})\b",
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
_DEPART_AT_RE = re.compile(
    r"(?:now\s+)?scheduled\s+to\s+depart\s+at\s+(\d{1,2}:\d{2}(?:\s*[AP]M)?)",
    re.IGNORECASE,
)
_CXL_NOT_AIRLINE_FAULT_RE = re.compile(
    r"ticketing\s+time\s+limit|payment\s+(?:was\s+)?not\s+(?:completed|received)"
    r"|unpaid|because\s+you\s+cancelled|cancelled\s+by\s+you|as\s+you\s+requested",
    re.IGNORECASE,
)

# Marketing / loyalty noise: never treat these as flight evidence unless
# they carry a booking reference.
_MARKETING_SENDER_RE = re.compile(
    r"^(?:offers?|myupgrades|newsletters?|news|promo(?:tions?)?|deals?|"
    r"marketing|campaign|specialoffers?|donotreply-offers)[@.]"
    r"|@(?:deals|offers|newsletter|email\.deals)\.|alfursanloyalty",
    re.IGNORECASE,
)
_MARKETING_SUBJECT_RE = re.compile(
    r"%\s*off|\boff(?:er|ers)\b.*\b(?:flight|fare)|voucher|\bsale\b|\bwin\b"
    r"|earn\s+miles|don'?t\s+miss|last\s+chance|exclusive|discount"
    r"|get\s+upgraded|bid\s+now",
    re.IGNORECASE,
)
# Hotel / non-flight senders whose "booking" emails are never flights.
_NON_FLIGHT_DOMAINS = (
    "agoda.com", "hilton.com", "booking.com", "marriott.com", "accor.com",
    "airbnb.com", "webook.com", "priceline.com", "hotels.com", "ihg.com",
)

# Segment-block scanning (line-oriented itinerary layouts).
_IATA_LINE_RE = re.compile(r"^\(?([A-Z]{3})\)?$")
_TIME_LINE_RE = re.compile(r"^(\d{1,2}:\d{2})\s*([AP]M)?$", re.IGNORECASE)
_DATE_LINE_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})$")
_SEG_LABEL_RE = re.compile(r"^(original|new)\s+flight$", re.IGNORECASE)

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
    passenger_profiles: dict[str, dict] = field(default_factory=dict)
    payment_method: str | None = None
    amount: str | None = None
    currency: str | None = None
    delay_hours: float | None = None
    new_departure: str | None = None
    new_arrival: str | None = None
    cancellation_reason: str | None = None
    segments: list[dict] = field(default_factory=list)
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


def _parse_date(text: str, email_dt: datetime | None) -> str | None:
    """Parse a date that may lack a year ("16 July") relative to when the
    email was sent: flights are booked ahead, not a year in the past."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip(" ,-:·")
    try:
        dt = dateparser.parse(cleaned, fuzzy=True, dayfirst=True,
                              default=email_dt or datetime.now())
    except (ValueError, OverflowError):
        return None
    if dt is None:
        return None
    has_year = bool(re.search(r"20\d{2}", cleaned))
    if email_dt and not has_year:
        if (email_dt - dt).days > 200:
            dt = dt.replace(year=dt.year + 1)
        elif (dt - email_dt).days > 330:
            dt = dt.replace(year=dt.year - 1)
    return dt.date().isoformat()


def _normalise_time(time_text: str) -> str | None:
    """'02:20AM' / '14:50' -> 'HH:MM' (24h)."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)?", time_text.strip(), re.IGNORECASE)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


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


def _flight_no_in_line(line: str) -> str | None:
    for code, number in _FLIGHT_RE.findall(line):
        code = code.upper()
        if code in _AIRLINE_CODES and code not in _FLIGHT_CODE_STOPWORDS:
            return f"{code}{int(number)}"
    return None


def _line_date(line: str, email_dt: datetime | None) -> str | None:
    for pattern in (_DEPARTING_DATE_RE, _WEEKDAY_DATE_RE, _DATE_LINE_RE):
        if m := pattern.search(line):
            if date := _parse_date(m.group(1), email_dt):
                return date
    return None


def _extract_segments(body: str, email_dt: datetime | None) -> list[dict]:
    """Scan line-oriented itinerary blocks like Saudia's:

        Departing · Friday, 23 May
        [Original flight | New flight]
        RUH
        Non-stop · 1h 35m
        MED
        15:00
        King Khalid International
        16:35
        Prince Mohammad Airport
        Saudia Airlines · SV 1463
    """
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    segments: list[dict] = []
    n = len(lines)
    i = 0
    while i < n:
        m = _IATA_LINE_RE.match(lines[i])
        if not m or m.group(1) in _NOT_AIRPORTS:
            i += 1
            continue
        origin = m.group(1)
        dest = dest_idx = None
        for k in range(i + 1, min(i + 7, n)):
            m2 = _IATA_LINE_RE.match(lines[k])
            if m2 and m2.group(1) not in _NOT_AIRPORTS and m2.group(1) != origin:
                dest, dest_idx = m2.group(1), k
                break
        if not dest:
            i += 1
            continue

        times: list[str] = []
        flight_no = None
        scan_end = dest_idx + 1
        for k in range(dest_idx + 1, min(dest_idx + 14, n)):
            if _IATA_LINE_RE.match(lines[k]) and _IATA_LINE_RE.match(lines[k]).group(1) not in _NOT_AIRPORTS:
                break  # next block starts
            scan_end = k + 1
            if len(times) < 2 and (mt := _TIME_LINE_RE.match(lines[k])):
                t = _normalise_time(mt.group(0))
                if t:
                    times.append(t)
            if not flight_no:
                flight_no = _flight_no_in_line(lines[k])

        label = seg_date = None
        for k in range(max(0, i - 8), i):
            if lm := _SEG_LABEL_RE.match(lines[k]):
                label = lm.group(1).lower()
            if d := _line_date(lines[k], email_dt):
                seg_date = d

        if times or flight_no:
            segments.append({
                "origin": origin,
                "destination": dest,
                "date": seg_date,
                "dep_time": times[0] if times else None,
                "arr_time": times[1] if len(times) > 1 else None,
                "flight_number": flight_no,
                "label": label,  # None | "original" | "new"
            })
        i = scan_end
    if segments:
        return segments
    return _extract_flyadeal_segments(lines, email_dt)


def _extract_flyadeal_segments(lines: list[str], email_dt: datetime | None) -> list[dict]:
    """flyadeal itineraries: '02:20AM' / '27 July 2024' line pairs, the
    route as 'AHB > RUH' and the flight as 'F3 28'."""
    route = None
    flight_no = None
    time_date_pairs: list[tuple[str, str]] = []
    for idx, line in enumerate(lines):
        if not route:
            if m := re.search(r"\b([A-Z]{3})\s*>\s*([A-Z]{3})\b", line):
                if m.group(1) not in _NOT_AIRPORTS and m.group(2) not in _NOT_AIRPORTS:
                    route = (m.group(1), m.group(2))
        if not flight_no:
            flight_no = _flight_no_in_line(line)
        if len(time_date_pairs) < 2 and _TIME_LINE_RE.match(line):
            if idx + 1 < len(lines) and (md := _DATE_LINE_RE.match(lines[idx + 1])):
                t = _normalise_time(line)
                d = _parse_date(md.group(1), email_dt)
                if t and d:
                    time_date_pairs.append((t, d))
    if not route:
        return []
    seg = {
        "origin": route[0],
        "destination": route[1],
        "date": time_date_pairs[0][1] if time_date_pairs else None,
        "dep_time": time_date_pairs[0][0] if time_date_pairs else None,
        "arr_time": time_date_pairs[1][0] if len(time_date_pairs) > 1 else None,
        "flight_number": flight_no,
        "label": None,
    }
    return [seg]


def segment_datetimes(seg: dict) -> tuple[str | None, str | None]:
    """(departure, arrival) ISO datetimes for a segment; arrivals that are
    earlier in the clock than the departure roll over to the next day."""
    date = seg.get("date")
    dep_time, arr_time = seg.get("dep_time"), seg.get("arr_time")
    if not date:
        return None, None
    dep = f"{date} {dep_time}" if dep_time else None
    arr = None
    if arr_time:
        arr_date = date
        if dep_time and arr_time < dep_time:  # overnight flight
            try:
                next_day = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
                arr_date = next_day.date().isoformat()
            except ValueError:
                pass
        arr = f"{arr_date} {arr_time}"
    return dep, arr


def _segment_dt(seg: dict, which: str) -> str | None:
    dep, arr = segment_datetimes(seg)
    return dep if which == "dep" else arr


def _clean_passenger_name(value: str) -> str:
    name = re.sub(r"\s+", " ", value or "").strip(" .,")
    while True:
        trimmed = _NAME_TRAILING_JUNK.sub("", name)
        if trimmed == name:
            break
        name = trimmed
    if name.isupper() or name.islower():
        name = name.title()
    return name


def _identity_key(value: str) -> str:
    value = re.sub(r"[^\w]+", " ", value or "", flags=re.UNICODE)
    return " ".join(value.casefold().split())


def _identity_source(text: str, position: int) -> str:
    markers = list(re.finditer(r"\[Attachment:\s*([^\]]+)\]", text[:position],
                               re.IGNORECASE))
    if markers:
        marker = markers[-1]
        return f"PDF attachment: {marker.group(1).strip()}"
    return "linked ticket or booking email"


def _extract_passenger_profiles(text: str) -> dict[str, dict]:
    """Extract identity fields within each passenger's own text block.

    Saudia commonly places several travelers in one itinerary.  Scoping from
    one titled name to the next prevents a sibling's loyalty or document
    number from being suggested for the wrong person.
    """
    people = list(_IDENTITY_PERSON_RE.finditer(text))
    profiles: dict[str, dict] = {}
    for index, match in enumerate(people):
        name = _clean_passenger_name(match.group("name"))
        key = _identity_key(name)
        if not key or name.casefold() in _NOT_NAMES:
            continue
        end = people[index + 1].start() if index + 1 < len(people) else len(text)
        block = text[match.start():min(end, match.start() + 1400)]
        source = _identity_source(text, match.start())
        profile = profiles.setdefault(key, {
            "booking_name": name,
            "full_name": name,
            "title": {"Miss": "Ms"}.get(match.group("title"),
                                          match.group("title")),
            "evidence": {},
        })
        evidence = profile.setdefault("evidence", {})
        identity_matches = list(_NATIONAL_ID_RE.finditer(block))
        identity_rank = {"national_id": 0, "iqama": 1, "passport": 2}

        def identity_type(value_match) -> str:
            label = value_match.group("label").casefold()
            if "passport" in label or "الجواز" in label:
                return "passport"
            if ("iqama" in label or "residence" in label
                    or "الإقامة" in label):
                return "iqama"
            return "national_id"

        preferred_identity = min(
            identity_matches,
            key=lambda item: identity_rank[identity_type(item)],
            default=None)
        found = {
            "alfursan_id": _ALFURSAN_ID_RE.search(block),
            "national_id": preferred_identity,
            "nationality": _NATIONALITY_RE.search(block),
            "email": _CONTACT_EMAIL_RE.search(block),
            "phone": _CONTACT_PHONE_RE.search(block),
        }
        for field, value_match in found.items():
            if not value_match:
                continue
            value = (value_match.group("value") if field == "national_id"
                     else value_match.group(1))
            value = " ".join(value.split()).strip(" .,:;-")
            if value and not profile.get(field):
                profile[field] = value
                evidence[field] = source
                if field == "national_id":
                    profile["national_id_type"] = identity_type(value_match)
        phone = profile.get("phone") or ""
        if phone.startswith("+") and (country := re.match(r"\+\d{1,3}", phone)):
            profile["country_code"] = country.group(0)
            evidence["country_code"] = evidence.get("phone", source)
    return profiles


def passenger_evidence_blocks(text: str, passenger_name: str,
                              limit: int = 8) -> list[dict]:
    """Return small, passenger-scoped source blocks for guarded AI review."""
    target = _identity_key(passenger_name)
    if not target:
        return []
    people = list(_IDENTITY_PERSON_RE.finditer(text or ""))
    blocks = []
    for index, match in enumerate(people):
        name = _clean_passenger_name(match.group("name"))
        if _identity_key(name) != target:
            continue
        end = people[index + 1].start() if index + 1 < len(people) else len(text)
        value = (text[match.start():min(end, match.start() + 1800)]).strip()
        if value:
            blocks.append({
                "source": _identity_source(text, match.start()),
                "text": value,
            })
        if len(blocks) >= limit:
            break
    if blocks:
        return blocks

    # Some airline layouts omit a title.  Use a narrow exact-name window and
    # never the whole multi-passenger document.
    simple = " ".join(str(passenger_name or "").split())
    for match in re.finditer(re.escape(simple), text or "", re.IGNORECASE):
        value = (text[max(0, match.start() - 100):match.start() + 1700]).strip()
        if value:
            blocks.append({
                "source": _identity_source(text, match.start()),
                "text": value,
            })
        if len(blocks) >= limit:
            break
    return blocks


def _extract_passenger(text: str) -> str | None:
    for pattern in (_PASSENGER_LABEL_RE, _PASSENGER_TITLE_RE, _PASSENGER_DEAR_RE):
        for m in pattern.finditer(text):
            name = _clean_passenger_name(m.group(1))
            if name.lower() in _NOT_NAMES or len(name) < 3:
                continue
            return name
    return None


def _extract_city_route(text: str) -> tuple[str, str, str, str] | None:
    """'Riyadh to Abha' -> (RUH, Riyadh, AHB, Abha) using the city table."""
    for m in _CITY_PAIR_RE.finditer(text):
        a, b = m.group(1).strip().lower(), m.group(2).strip().lower()
        # try the full phrase, then the last word ("trip to Riyadh" -> no)
        code_a = CITY_IATA.get(a) or CITY_IATA.get(a.split()[-1])
        code_b = CITY_IATA.get(b) or CITY_IATA.get(b.split()[-1])
        if code_a and code_b and code_a != code_b:
            return code_a, m.group(1).strip().title(), code_b, m.group(2).strip().title()
    return None


def is_marketing(subject: str, sender: str) -> bool:
    local_and_domain = sender or ""
    return bool(_MARKETING_SENDER_RE.search(local_and_domain)
                or _MARKETING_SUBJECT_RE.search(subject or ""))


def parse_email(message_id: str, subject: str, sender: str, date: datetime | None,
                body: str) -> ParsedEmail | None:
    """Parse one email. Returns None when it is clearly not flight-related."""
    body = clean_email_body(body)
    sender_domain = sender.split("@")[-1].strip("> ").lower() if "@" in sender else ""
    if any(sender_domain == d or sender_domain.endswith("." + d)
           for d in _NON_FLIGHT_DOMAINS):
        return None
    airline_code, airline = airline_for_domain(sender_domain)
    if not airline_code:
        airline_code, airline = airline_for_name(f"{subject}\n{body[:2000]}")

    kinds = classify(subject, body)
    text = f"{subject}\n{body}"
    flights = _extract_flights(text)

    if not airline_code and flights:
        airline_code = flights[0][:2]
        airline = AIRLINES.get(airline_code)

    pnr_match = _PNR_RE.search(text)

    # Marketing / loyalty mail without a booking reference is noise, even
    # when it comes from an airline domain.
    if is_marketing(subject, sender) and not pnr_match:
        return None
    # Not an airline email at all -> skip.
    if not airline_code and not kinds:
        return None
    # Non-airline senders (hotels, OTAs, calendars) need real booking
    # evidence, not just the word "booking" in the subject.
    if not airline_code and not flights and not pnr_match:
        return None
    if not kinds and not flights and not pnr_match:
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

    if pnr_match:
        candidate = pnr_match.group(1).upper()
        # PNRs are alphanumeric; reject pure long digit runs (phone bits
        # etc.) and English words that slipped past the label match.
        if (candidate not in _PNR_STOPWORDS
                and (not candidate.isdigit() or len(candidate) == 6)):
            parsed.pnr = candidate

    tickets = [t.replace(" ", "-") for t in _TICKET_RE.findall(text)]
    tickets += [t for t in _TICKET_BARE_RE.findall(text) if t not in tickets]
    parsed.ticket_numbers = list(dict.fromkeys(
        t if "-" in t else f"{t[:3]}-{t[3:]}" for t in tickets))

    base_date = date.replace(tzinfo=None) if date else None

    # --- Route ---
    if m := _ROUTE_NAMED_RE.search(text):
        parsed.origin_city = m.group(1).strip()
        parsed.origin = m.group(2)
        parsed.destination_city = m.group(3).strip()
        parsed.destination = m.group(4)
    elif m := _ROUTE_CODES_RE.search(text):
        if m.group(1) not in _NOT_AIRPORTS and m.group(2) not in _NOT_AIRPORTS:
            parsed.origin, parsed.destination = m.group(1), m.group(2)
    if not parsed.origin:
        if city_route := _extract_city_route(f"{subject}\n{body[:1500]}"):
            parsed.origin, parsed.origin_city = city_route[0], city_route[1]
            parsed.destination, parsed.destination_city = city_route[2], city_route[3]

    # --- Itinerary segments (most reliable source of route/times/date) ---
    parsed.segments = _extract_segments(body, base_date)
    for seg in parsed.segments:
        if seg.get("flight_number") and seg["flight_number"] not in parsed.flight_numbers:
            parsed.flight_numbers.append(seg["flight_number"])

    subject_flight = _flight_no_in_line(subject)
    plain_segs = [s for s in parsed.segments if s.get("label") != "new"]
    primary = None
    if subject_flight:
        primary = next((s for s in plain_segs
                        if s.get("flight_number") == subject_flight), None)
    if primary is None and plain_segs:
        primary = plain_segs[0]
    if primary:
        if parsed.origin != primary["origin"]:
            parsed.origin_city = None
        if parsed.destination != primary["destination"]:
            parsed.destination_city = None
        parsed.origin = primary["origin"]
        parsed.destination = primary["destination"]
        if primary.get("date"):
            parsed.flight_date = primary["date"]
        parsed.departure = _segment_dt(primary, "dep") or parsed.departure
        parsed.arrival = _segment_dt(primary, "arr") or parsed.arrival
        new_seg = next((s for s in parsed.segments if s.get("label") == "new"
                        and (not primary.get("flight_number")
                             or s.get("flight_number") == primary.get("flight_number"))),
                       None)
        if new_seg:
            parsed.new_departure = _segment_dt(new_seg, "dep")
            parsed.new_arrival = _segment_dt(new_seg, "arr")

    # --- Dates (never "Booking date") ---
    if not parsed.flight_date:
        if m := _DATE_RE.search(text):
            parsed.flight_date = _parse_date(m.group(1), base_date)
    if not parsed.flight_date:
        if m := _DEPARTING_DATE_RE.search(text):
            parsed.flight_date = _parse_date(m.group(1), base_date)
    if not parsed.flight_date:
        if m := _SUBJECT_ON_DATE_RE.search(subject):
            parsed.flight_date = _parse_date(m.group(1), base_date)
    if not parsed.flight_date:
        if m := _WEEKDAY_DATE_RE.search(body[:2500]):
            parsed.flight_date = _parse_date(m.group(1), base_date)

    if not parsed.departure and (m := _DEPARTURE_RE.search(text)):
        parsed.departure = _try_parse_dt(m.group(1), default=base_date)
    if not parsed.arrival and (m := _ARRIVAL_RE.search(text)):
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
    parsed.passenger = _extract_passenger(text)
    parsed.passenger_profiles = _extract_passenger_profiles(text)

    if m := _PAYMENT_RE.search(text):
        method = re.sub(r"\s+", " ", m.group(1)).title()
        method = {"Amex": "American Express", "Master Card": "Mastercard",
                  "Mada": "MADA", "Stc Pay": "STC Pay"}.get(method, method)
        nearby = text[m.start():m.start() + 140]
        last_four_match = _CARD_LAST4_RE.search(nearby)
        last_four = last_four_match.group(1) if last_four_match else ""
        parsed.payment_method = method + (f" •••• {last_four}" if last_four else "")
    if m := _AMOUNT_RE.search(text):
        parsed.currency = (m.group(1) or m.group(3) or "").upper() or None
        parsed.amount = m.group(2)

    if m := _DELAY_HOURS_RE.search(text):
        parsed.delay_hours = float(m.group(1))
    if not parsed.new_departure and (m := _NEW_DEPARTURE_RE.search(text)):
        parsed.new_departure = _try_parse_dt(m.group(1), default=base_date)
    if not parsed.new_departure and DELAY in kinds and (m := _DEPART_AT_RE.search(text)):
        # "is delayed, and is now scheduled to depart at 14:50"
        t = _normalise_time(m.group(1))
        if t and parsed.flight_date:
            parsed.new_departure = f"{parsed.flight_date} {t}"
    if not parsed.new_arrival and (m := _NEW_ARRIVAL_RE.search(text)):
        parsed.new_arrival = _try_parse_dt(m.group(1), default=base_date)

    if CANCELLATION in kinds and _CXL_NOT_AIRLINE_FAULT_RE.search(text):
        parsed.cancellation_reason = "not_airline_fault"

    return parsed
