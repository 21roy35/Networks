"""Compensation eligibility assessment.

Primary framework: GACA's Customer Protection Regulation (Saudi Arabia,
in force since 20 November 2023) which covers flights departing from,
arriving to, or operated by carriers licensed in Saudi Arabia.
Secondary framework: EU Regulation 261/2004 for flights departing the
EU/UK or operated by EU/UK carriers.

This module produces guidance, not legal advice: it grades each flight
as eligible / possibly eligible / not eligible / insufficient data and
explains why, based on the delay at arrival, cancellation notice period
and other captured facts.
"""

from datetime import datetime

from .airlines import EU_CARRIERS, SAUDI_CARRIERS

ELIGIBLE = "eligible"
POSSIBLY = "possibly"
NOT_ELIGIBLE = "not_eligible"
UNKNOWN = "unknown"

VERDICT_LABELS = {
    ELIGIBLE: "Likely eligible for compensation",
    POSSIBLY: "Possibly eligible — needs more info",
    NOT_ELIGIBLE: "Not eligible",
    UNKNOWN: "Insufficient data",
}

# Saudi airport IATA codes (GACA applies to any flight touching KSA).
_SAUDI_AIRPORTS = {
    "RUH", "JED", "DMM", "MED", "AHB", "TUU", "TIF", "GIZ", "ELQ", "HAS",
    "AJF", "RAE", "YNB", "EAM", "BHH", "ABT", "DWD", "AQI", "WAE", "URY",
    "HOF", "GYE", "KMX", "SHW", "AKH", "NUM", "RED",
}

_EU_UK_PREFIX_AIRPORTS = set()  # kept simple; carrier + origin check below


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:16], fmt)
        except ValueError:
            continue
    return None


def effective(flight: dict, key: str):
    """Override-aware field access (manual GUI corrections win)."""
    overrides = flight.get("overrides") or {}
    return overrides.get(key) or flight.get(key)


def arrival_delay_hours(flight: dict) -> float | None:
    """Best available estimate of the delay at arrival, in hours."""
    override = effective(flight, "actual_arrival")
    scheduled = _parse_dt(flight.get("arrival"))
    actual = _parse_dt(override) or _parse_dt(flight.get("new_arrival"))
    if scheduled and actual:
        return round((actual - scheduled).total_seconds() / 3600, 1)
    if flight.get("delay_hours") is not None:
        return float(flight["delay_hours"])
    return None


def assess(flight: dict) -> dict:
    """Return {verdict, label, reasons, frameworks, delay_hours}."""
    reasons: list[str] = []
    frameworks: list[str] = []
    verdict = UNKNOWN

    airline = flight.get("airline_code")
    origin = effective(flight, "origin")
    destination = effective(flight, "destination")
    delay = arrival_delay_hours(flight)
    cancelled = flight.get("cancelled") or (effective(flight, "cancelled") is True)
    notice_days = flight.get("overrides", {}).get("cancellation_notice_days")
    denied_boarding = flight.get("overrides", {}).get("denied_boarding")

    gaca_applies = (airline in SAUDI_CARRIERS
                    or origin in _SAUDI_AIRPORTS
                    or destination in _SAUDI_AIRPORTS)
    eu_applies = airline in EU_CARRIERS  # or EU origin (add codes if needed)

    if gaca_applies:
        frameworks.append("GACA Customer Protection Regulation (KSA)")
    if eu_applies:
        frameworks.append("EU Regulation 261/2004")
    if not frameworks:
        frameworks.append("Airline's own conditions of carriage")
        reasons.append(
            "Flight does not clearly fall under GACA or EU261 based on the "
            "captured route/carrier; the airline's own policy applies.")

    if cancelled:
        if notice_days is not None and float(notice_days) >= 14:
            verdict = NOT_ELIGIBLE
            reasons.append(
                f"Flight was cancelled but with {notice_days} days' notice "
                "(14+ days generally removes the right to compensation, "
                "though a full refund is still due).")
        else:
            verdict = ELIGIBLE if gaca_applies or eu_applies else POSSIBLY
            reasons.append(
                "Flight was cancelled. Under the GACA regulation, cancellation "
                "without sufficient prior notice entitles the passenger to "
                "compensation of 100–200% of the ticket value in addition to "
                "a refund or re-routing. Under EU261 (if applicable), fixed "
                "compensation of EUR 250–600 applies unless 14+ days' notice "
                "was given or extraordinary circumstances apply.")
            if notice_days is None:
                reasons.append(
                    "Enter the cancellation notice period in the flight page "
                    "to firm this up (less than 14 days strengthens the claim).")
    elif denied_boarding:
        verdict = ELIGIBLE if gaca_applies or eu_applies else POSSIBLY
        reasons.append(
            "Denied boarding (e.g. overbooking) entitles the passenger to "
            "compensation under both the GACA regulation (100–200% of ticket "
            "value) and EU261.")
    elif delay is not None:
        if delay >= 6 and gaca_applies:
            verdict = ELIGIBLE
            reasons.append(
                f"Arrival delay of ~{delay:g} hours. Under the GACA regulation, "
                "a delay of 6 hours or more entitles the passenger to "
                "compensation of 100–200% of the ticket value, plus care "
                "(meals, communication, and accommodation if overnight).")
        elif delay >= 3:
            verdict = ELIGIBLE if eu_applies else POSSIBLY
            reasons.append(
                f"Arrival delay of ~{delay:g} hours. Under EU261 a 3+ hour "
                "arrival delay triggers EUR 250–600 compensation (unless "
                "extraordinary circumstances). Under GACA rules a 3–6 hour "
                "delay triggers care obligations (meals, refreshments, "
                "communication) and a refund if you chose not to travel; "
                "monetary compensation generally starts at 6 hours.")
        elif delay >= 1:
            verdict = NOT_ELIGIBLE
            reasons.append(
                f"Arrival delay of ~{delay:g} hours is below the 3-hour "
                "threshold for monetary compensation, though basic care "
                "(refreshments/communication) may still have been due.")
        else:
            verdict = NOT_ELIGIBLE
            reasons.append("No material delay detected for this flight.")
    else:
        reasons.append(
            "No delay or cancellation information was found in the emails. "
            "If the flight actually landed late, enter the actual arrival "
            "time on this page and re-check.")

    if verdict in (ELIGIBLE, POSSIBLY):
        reasons.append(
            "Note: compensation can be reduced or excluded for extraordinary "
            "circumstances outside the airline's control (severe weather, "
            "ATC restrictions, security risks). This assessment is guidance, "
            "not legal advice.")

    return {
        "verdict": verdict,
        "label": VERDICT_LABELS[verdict],
        "reasons": reasons,
        "frameworks": frameworks,
        "delay_hours": delay,
    }
