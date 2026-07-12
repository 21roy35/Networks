"""Link related emails into flight records.

Emails are grouped when they share a PNR, an e-ticket number, or the same
flight number on the same date. Each group is then merged into one flight
record, with the most authoritative email winning for each field
(e.g. the boarding-pass email wins for seat/gate, the e-ticket email wins
for the ticket number, delay/cancellation notices win for status).
"""

from collections import defaultdict

from .parser import (BOARDING_PASS, BOOKING, CANCELLATION, CHECKIN, DELAY,
                     ETICKET, GATE_CHANGE, KIND_LABELS, RECEIPT)

# Fields merged from member emails, in the order they appear in the GUI.
MERGE_FIELDS = [
    "airline_code", "airline_name", "pnr", "origin", "origin_city",
    "destination", "destination_city", "flight_date", "departure", "arrival",
    "cabin_class", "seat", "gate", "boarding_time", "passenger",
    "payment_method", "amount", "currency", "delay_hours",
    "new_departure", "new_arrival",
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


def link_emails(emails: list[dict]) -> list[dict]:
    """Group parsed emails and merge each group into a flight record."""
    flights = []
    seen_keys = set()
    for group in _group_emails(emails):
        flight = _merge_group(group)
        # Guard against duplicate keys from degenerate parses.
        while flight["flight_key"] in seen_keys:
            flight["flight_key"] += "+"
        seen_keys.add(flight["flight_key"])
        flights.append(flight)
    return flights
