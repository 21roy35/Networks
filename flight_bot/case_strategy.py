"""Deterministic next-action recommendations for passenger complaint cases."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .compensation import ELIGIBLE, POSSIBLY, assess, effective
from .flight_status import parse_flight_time


def incident_category(text: str, cancelled: bool = False) -> str:
    value = str(text or "").casefold()
    rules = (
        ("baggage", r"\b(?:bag|baggage|luggage|suitcase|lost bag|damag\w* bag)\b"),
        ("entertainment", r"\b(?:screen|entertainment|ife|monitor|headphone)\b"),
        ("seat", r"\b(?:seat|recline|tray table|armrest)\b"),
        ("delay", r"\b(?:delay|late|waiting|hours? late)\b"),
        ("cancellation", r"\b(?:cancel|cancelled|canceled|rebook)\b"),
        ("refund", r"\b(?:refund|reimburse|money back|charge)\b"),
        ("accessibility", r"\b(?:wheelchair|accessib|disability|special assistance)\b"),
        ("service", r"\b(?:rude|service|staff|crew|meal|food)\b"),
    )
    for category, pattern in rules:
        if re.search(pattern, value):
            return category
    return "cancellation" if cancelled else "other"


def _incident_text(complaints: list[dict]) -> str:
    return "\n".join(str(item.get("details") or "") for item in complaints
                     if item.get("details"))


def _latest(items: list[dict], kind: str) -> dict | None:
    selected = [item for item in items if item.get("kind") == kind]
    return selected[-1] if selected else None


def _tailored_remedy(category: str, assessment: dict) -> str:
    remedies = assessment.get("remedies") or []
    if remedies:
        return "; ".join(remedies)
    return {
        "baggage": "Repair, replacement, or reimbursement supported by baggage and purchase evidence",
        "entertainment": "A proportionate refund, compensation, or goodwill remedy for the failed service",
        "seat": "A proportionate refund or compensation for the defective seat/service",
        "delay": "Applicable delay compensation plus reimbursement of reasonable documented expenses",
        "cancellation": "Refund or rerouting plus applicable cancellation/delay compensation",
        "refund": "Prompt refund to the original payment method and reimbursement of proven loss",
        "accessibility": "Investigation, corrective action, and any applicable compensation",
        "service": "Investigation and a proportionate service-recovery remedy",
        "other": "Investigation and the strongest applicable remedy supported by the evidence",
    }[category]


def _evidence_checklist(category: str) -> list[str]:
    common = ["Booking confirmation or ticket", "Flight number and travel date"]
    specific = {
        "baggage": ["Baggage tag and airport damage/loss report", "Photos and repair/replacement receipt"],
        "entertainment": ["Photo/video of the broken screen", "Seat number and when crew were notified"],
        "seat": ["Photo/video of the defect", "Seat number and when crew were notified"],
        "delay": ["Airline delay notice", "Actual arrival time and expense receipts"],
        "cancellation": ["Cancellation notice with timestamp", "Rebooking/refund details and expense receipts"],
        "refund": ["Payment proof", "Refund request and promised processing date"],
        "accessibility": ["Assistance request confirmation", "Timeline, witnesses, and related receipts"],
        "service": ["Seat/flight details", "Names or description and contemporaneous notes"],
        "other": ["Photos, messages, receipts, or contemporaneous notes"],
    }
    return common + specific[category]


def recommend_case(flight: dict, snapshot: dict | None = None,
                   complaints: list[dict] | None = None,
                   responses: list[dict] | None = None,
                   now: datetime | None = None, gaca_days: int = 7) -> dict:
    """Return one bounded recommendation with reasons, gaps, evidence, and deadline."""
    now = now or datetime.now()
    snapshot = snapshot or {}
    complaints = list(complaints if complaints is not None
                      else flight.get("complaints") or [])
    responses = list(responses or [])
    working = dict(flight)
    working["overrides"] = dict(flight.get("overrides") or {})
    if snapshot.get("actual_arrival") and not effective(working, "actual_arrival"):
        working["overrides"]["actual_arrival"] = snapshot["actual_arrival"]
    if snapshot.get("status") == "cancelled":
        working["overrides"]["cancelled"] = True
    assessment = assess(working)
    incident = _incident_text(complaints)
    category = incident_category(
        incident, bool(effective(working, "cancelled")))
    airline = _latest(complaints, "airline")
    gaca = _latest(complaints, "gaca")
    gaca_applies = any("GACA" in item for item in assessment.get("frameworks") or [])
    status = snapshot.get("status") or "unknown"
    reasons = []
    missing = []
    action = "collect_evidence"
    next_review_at = None

    closed_airline = airline and airline.get("status") in {"closed", "resolved"}
    if snapshot.get("contradictions"):
        action = "manual_review"
        reasons.extend(snapshot["contradictions"])
    elif gaca and gaca.get("status") in {"filing", "submitted", "filed", "sent"}:
        action = "wait_regulator_response"
        reasons.append("A GACA escalation is already active, so another filing would duplicate it.")
    elif closed_airline:
        # One-shot filing must not trap the case after the airline closes it.
        if gaca_applies and responses:
            action = "escalate_gaca"
            reasons.append(
                "The airline closed or resolved the prior complaint; escalate to GACA "
                "or reopen a fresh airline filing if the remedy is still unsatisfactory.")
        else:
            action = "reopen_airline"
            reasons.append(
                "The prior airline complaint is closed or resolved; a new complaint "
                "can be filed if the passenger still needs a remedy.")
    elif airline and airline.get("status") in {
            "filing", "submitted", "filed", "sent", "accepted_pending_reference"}:
        created = parse_flight_time(airline.get("created_at"))
        due = created + timedelta(days=max(1, int(gaca_days))) if created else None
        if responses:
            action = "review_airline_response"
            reasons.append("The airline has responded; compare its proposed resolution with the claim and evidence.")
        elif not airline.get("reference"):
            action = "recover_airline_reference"
            reasons.append("The airline complaint is recorded but GACA cannot accept an escalation without its reference.")
            next_review_at = due.isoformat() if due else None
        elif gaca_applies and due and now >= due:
            action = "escalate_gaca"
            reasons.append("The airline complaint has a reference and the configured seven-day GACA response period has elapsed.")
        else:
            action = "wait_airline_response"
            reasons.append("The airline complaint is already active; monitor for a substantive response before duplicating it.")
            next_review_at = due.isoformat() if due else None
    elif status in {"scheduled", "due", "boarding", "departed", "airborne", "delayed"}:
        action = "wait_for_arrival"
        reasons.append("Wait for the final arrival outcome so the actual delay and remedy can be calculated accurately.")
        if status == "delayed":
            reasons.append("Preserve the airline delay notice and receipts while waiting.")
    elif bool(effective(working, "cancelled")):
        action = "ask_missing_fact" if not incident else "file_airline_now"
        reasons.append("A cancellation should be raised with the airline first once rebooking/refund facts are recorded.")
        if not (working.get("overrides") or {}).get("accepted_alternative"):
            missing.append("Whether the passenger accepted an alternative flight")
        if (working.get("overrides") or {}).get("cancellation_notice_days") in {None, ""}:
            missing.append("How many days before departure the cancellation was communicated")
    elif not incident:
        action = "collect_evidence"
        reasons.append("No passenger incident has been recorded for this flight yet.")
        missing.append("What went wrong and how it affected the passenger")
    else:
        action = "file_airline_now"
        reasons.append("The flight is complete and an incident is recorded; the airline complaint is the next step.")

    if assessment.get("verdict") in {ELIGIBLE, POSSIBLY}:
        rights_reasons = assessment.get("reasons") or []
        if rights_reasons:
            reasons.append(rights_reasons[0])
    if category in {"delay", "cancellation"} and not snapshot.get("actual_arrival"):
        missing.append("Verified actual arrival time or replacement-flight arrival time")
    if not effective(working, "origin") or not effective(working, "destination"):
        missing.append("Complete origin and destination airport codes")
    deadline = None
    incident_day = parse_flight_time(effective(working, "flight_date"))
    if gaca_applies and incident_day:
        deadline = (incident_day + timedelta(days=60)).date().isoformat()

    evidence = _evidence_checklist(category)
    available = 2
    if incident:
        available += 2
    if snapshot.get("provider") not in {None, "", "none", "schedule"}:
        available += 2
    if any(item.get("attachments") for item in complaints):
        available += 2
    if airline and airline.get("reference"):
        available += 2
    readiness = max(0, min(100, available * 10 - len(missing) * 10))
    return {
        "recommended_action": action,
        "label": action.replace("_", " ").title(),
        "confidence": snapshot.get("confidence") or .5,
        "category": category,
        "reasons": reasons,
        "missing_facts": list(dict.fromkeys(missing)),
        "evidence_checklist": evidence,
        "requested_remedy": _tailored_remedy(category, assessment),
        "next_review_at": next_review_at,
        "filing_deadline": deadline,
        "readiness_score": readiness,
        "rights": assessment,
        "status": snapshot,
    }
