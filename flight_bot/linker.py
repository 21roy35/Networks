"""Link related emails into flight records.

Emails are grouped when they share a PNR, an e-ticket number, or the same
flight number on the same date. Each group is then merged into one flight
record, with the most authoritative email winning for each field
(e.g. the boarding-pass email wins for seat/gate, the e-ticket email wins
for the ticket number, delay/cancellation notices win for status).
"""

from collections import defaultdict

from .parser import (BOARDING_PASS, BOOKING, CANCELLATION, CHECKIN, DELAY,
                     ETICKET, GATE_CHANGE, KIND_LABELS, RECEIPT,
                     segment_datetimes)

# Fields merged from member emails, in the order they appear in the GUI.
MERGE_FIELDS = [
    "airline_code", "airline_name", "pnr", "origin", "origin_city",
    "destination", "destination_city", "flight_date", "departure", "arrival",
    "cabin_class", "seat", "gate", "boarding_time", "passenger",
    "payment_method", "amount", "currency", "delay_hours",
    "new_departure", "new_arrival", "cancellation_reason",
]

# Which email kinds are most trustworthy for which fields.
_FIELD_PRIORITY = {
    "seat": [BOARDING_PASS, CHECKIN, ETICKET, BOOKING],
    "gate": [GATE_CHANGE, BOARDING_PASS, CHECKIN],
    "boarding_time": [BOARDING_PASS, CHECKIN, DELAY],
    "payment_method": [RECEIPT, BOOKING, ETICKET],
    "amount": [RECEIPT, BOOKING, ETICKET],
    "currency": [RECEIPT, BOOKING, ETICKET],
    "departure": [ETICKET, BOOKING, BOARDING_PASS, CHECKIN],
    "arrival": [BOARDING_PASS, ETICKET, BOOKING],
    "delay_hours": [DELAY],
    "new_departure": [DELAY],
    "new_arrival": [DELAY],
}
_DEFAULT_PRIORITY = [ETICKET, BOARDING_PASS, BOOKING, CHECKIN, RECEIPT,
                     DELAY, CANCELLATION]


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _group_emails(emails: list[dict]) -> list[list[dict]]:
    """Union emails that share a PNR, ticket number, or flight+date."""
    uf = _UnionFind(len(emails))
    by_key = defaultdict(list)
    for i, email in enumerate(emails):
        if email.get("pnr"):
            by_key[("pnr", email["pnr"])].append(i)
        for ticket in email.get("ticket_numbers") or []:
            by_key[("ticket", ticket)].append(i)
        date = (email.get("flight_date") or (email.get("departure") or "").split(" ")[0]
                or (email.get("date") or "").split("T")[0])
        for flight_no in email.get("flight_numbers") or []:
            if date:
                by_key[("flight", flight_no, date)].append(i)
    for indices in by_key.values():
        for other in indices[1:]:
            uf.union(indices[0], other)

    groups = defaultdict(list)
    for i, email in enumerate(emails):
        groups[uf.find(i)].append(email)
    return list(groups.values())


def _pick(group: list[dict], field: str):
    """Pick the field value from the most authoritative email in the group."""
    priority = _FIELD_PRIORITY.get(field, _DEFAULT_PRIORITY)
    ranked = []
    for email in group:
        value = email.get(field)
        if value in (None, "", []):
            continue
        kinds = email.get("kinds") or []
        rank = min((priority.index(k) for k in kinds if k in priority),
                   default=len(priority))
        # Later emails break ties (more recent info wins).
        ranked.append((rank, -(email.get("db_id") or 0), value))
    if not ranked:
        return None
    return min(ranked)[2]


def _merge_group(group: list[dict]) -> dict:
    flight = {field: _pick(group, field) for field in MERGE_FIELDS}

    tickets, kinds_present, flight_numbers = [], [], []
    for email in sorted(group, key=lambda e: e.get("date") or ""):
        for t in email.get("ticket_numbers") or []:
            if t not in tickets:
                tickets.append(t)
        for k in email.get("kinds") or []:
            if k not in kinds_present:
                kinds_present.append(k)
        for f in email.get("flight_numbers") or []:
            if f not in flight_numbers:
                flight_numbers.append(f)

    flight["ticket_numbers"] = tickets
    flight["flight_numbers"] = flight_numbers
    flight["flight_number"] = flight_numbers[0] if flight_numbers else None
    flight["kinds"] = kinds_present
    flight["kind_labels"] = [KIND_LABELS.get(k, k) for k in kinds_present]
    flight["cancelled"] = CANCELLATION in kinds_present
    flight["has_boarding_pass"] = BOARDING_PASS in kinds_present
    flight["email_ids"] = [e["db_id"] for e in group if e.get("db_id")]
    flight["email_count"] = len(group)

    if not flight.get("flight_date"):
        dep = flight.get("departure") or ""
        flight["flight_date"] = dep.split(" ")[0] if dep else None

    key_parts = [flight.get("pnr") or (tickets[0] if tickets else None)
                 or "unknown",
                 flight.get("flight_number") or "?",
                 flight.get("flight_date") or "?"]
    flight["flight_key"] = "|".join(str(p) for p in key_parts)
    return flight


def _is_real_flight(flight: dict) -> bool:
    """Filter out airline marketing/newsletter noise.

    A group only counts as a flight when it carries booking evidence:
    a PNR, an e-ticket number, or a flight number together with at least
    one concrete travel fact (route, departure time or flight date).
    """
    if flight.get("pnr") or flight.get("ticket_numbers"):
        return True
    return bool(flight.get("flight_number")
                and (flight.get("origin") or flight.get("destination")
                     or flight.get("departure") or flight.get("flight_date")))


def _compatible_pnr(a: dict, b: dict) -> bool:
    return not a.get("pnr") or not b.get("pnr") or a["pnr"] == b["pnr"]


def _segment_flights(base: dict, group: list[dict]) -> list[dict]:
    """Split a merged booking into one flight record per itinerary segment.

    A round trip booked under one PNR contains several segments; each
    becomes its own flight so every leg gets a route and its own
    delay/cancellation assessment. Reschedule notices contribute
    original (scheduled) and new times to the same segment.
    """
    clusters: dict = {}
    order: list = []
    for email in sorted(group, key=lambda e: e.get("date") or ""):
        for seg in email.get("segments") or []:
            key = seg.get("flight_number") or (seg.get("origin"), seg.get("destination"))
            if key not in clusters:
                clusters[key] = {"origin": None, "destination": None,
                                 "date": None, "departure": None,
                                 "arrival": None, "new_departure": None,
                                 "new_arrival": None, "flight_number": None}
                order.append(key)
            c = clusters[key]
            c["origin"] = c["origin"] or seg.get("origin")
            c["destination"] = c["destination"] or seg.get("destination")
            c["flight_number"] = c["flight_number"] or seg.get("flight_number")
            dep, arr = segment_datetimes(seg)
            if seg.get("label") == "new":
                # latest reschedule wins
                c["new_departure"] = dep or c["new_departure"]
                c["new_arrival"] = arr or c["new_arrival"]
            else:
                c["date"] = c["date"] or seg.get("date")
                c["departure"] = c["departure"] or dep
                c["arrival"] = c["arrival"] or arr

    if not clusters:
        return [base]

    flights = []
    for key in order:
        c = clusters[key]
        flight = dict(base)
        flight["origin"] = c["origin"]
        flight["destination"] = c["destination"]
        if flight.get("origin_city") and len(clusters) > 1:
            # city names from the merged base may belong to another leg
            flight["origin_city"] = flight["destination_city"] = None
        flight["flight_date"] = c["date"] or base.get("flight_date")
        flight["departure"] = c["departure"] or (base.get("departure") if len(clusters) == 1 else None)
        flight["arrival"] = c["arrival"] or (base.get("arrival") if len(clusters) == 1 else None)
        flight["new_departure"] = c["new_departure"] or (base.get("new_departure") if len(clusters) == 1 else None)
        flight["new_arrival"] = c["new_arrival"] or (base.get("new_arrival") if len(clusters) == 1 else None)
        if len(clusters) > 1:
            flight["delay_hours"] = None  # per-leg; recomputed from times
        if c["flight_number"]:
            flight["flight_number"] = c["flight_number"]
            flight["flight_numbers"] = [c["flight_number"]]
        key_parts = [flight.get("pnr") or "unknown",
                     flight.get("flight_number") or "?",
                     flight.get("flight_date") or "?"]
        flight["flight_key"] = "|".join(str(p) for p in key_parts)
        flights.append(flight)
    return flights


def _merge_flight_records(a: dict, b: dict) -> dict:
    """Combine two records describing the same physical flight."""
    out = dict(a)
    for key, value in b.items():
        if out.get(key) in (None, "", []):
            out[key] = value
    for list_key in ("ticket_numbers", "flight_numbers", "kinds", "email_ids"):
        seen = list(a.get(list_key) or [])
        for v in b.get(list_key) or []:
            if v not in seen:
                seen.append(v)
        out[list_key] = seen
    out["kind_labels"] = [KIND_LABELS.get(k, k) for k in out.get("kinds") or []]
    out["cancelled"] = a.get("cancelled") or b.get("cancelled")
    out["has_boarding_pass"] = a.get("has_boarding_pass") or b.get("has_boarding_pass")
    out["email_count"] = len(out.get("email_ids") or [])
    return out


def link_emails(emails: list[dict], log=lambda *a: None) -> list[dict]:
    """Group parsed emails and merge each group into flight records."""
    # Pass 1: group by shared PNR / ticket number / flight+date, then
    # split every group into per-segment flights.
    records: list[dict] = []
    for group in _group_emails(emails):
        base = _merge_group(group)
        records.extend(_segment_flights(base, group))

    # Pass 2: coalesce records that resolve to the same physical flight
    # (same flight number on the same date), unless they carry
    # conflicting booking references.
    merged: list[dict] = []
    for flight in records:
        for i, other in enumerate(merged):
            if (flight.get("flight_number") and flight.get("flight_date")
                    and flight["flight_number"] == other.get("flight_number")
                    and flight["flight_date"] == other.get("flight_date")
                    and _compatible_pnr(flight, other)):
                merged[i] = _merge_flight_records(other, flight)
                break
        else:
            merged.append(flight)

    flights, seen_keys, dropped = [], set(), 0
    for flight in merged:
        if not _is_real_flight(flight):
            dropped += 1
            continue
        key_parts = [flight.get("pnr") or (flight.get("ticket_numbers") or ["unknown"])[0],
                     flight.get("flight_number") or "?",
                     flight.get("flight_date") or "?"]
        flight["flight_key"] = "|".join(str(p) for p in key_parts)
        # Guard against duplicate keys from degenerate parses.
        while flight["flight_key"] in seen_keys:
            flight["flight_key"] += "+"
        seen_keys.add(flight["flight_key"])
        flights.append(flight)
    if dropped:
        log(f"  (ignored {dropped} email group(s) with no booking evidence "
            "— likely promotions/newsletters)")
    return flights
