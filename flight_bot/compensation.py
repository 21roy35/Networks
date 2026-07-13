"""Conservative passenger-rights assessment for a linked flight.

The result is deliberately phrased as guidance rather than a legal decision.
Rules and scope are based on the current official GACA passenger-rights page
and the EU air-passenger-rights guidance, verified on 2026-07-13.
"""

from datetime import datetime

from .airlines import EU_EEA_CARRIERS, SAUDI_CARRIERS, UK_CARRIERS

ELIGIBLE = "eligible"
POSSIBLY = "possibly"
NOT_ELIGIBLE = "not_eligible"
UNKNOWN = "unknown"

VERDICT_LABELS = {
    ELIGIBLE: "Likely claim",
    POSSIBLY: "Review recommended",
    NOT_ELIGIBLE: "No cash claim detected",
    UNKNOWN: "More details needed",
}

GACA_SOURCE = "https://gaca.gov.sa/ar/passenger-rights"
EU_SOURCE = "https://europa.eu/youreurope/citizens/travel/passenger-rights/air/index_en.htm"
UK_SOURCE = "https://www.caa.co.uk/air-passengers/travel-problems-and-rights/flight-delays-and-cancellations/delays/"

# GACA applies to departures from Saudi airports, plus arrivals to Saudi
# Arabia when operated by a Saudi carrier. Foreign-carrier arrivals are not
# generally in scope (apart from the regulation's separate baggage rules).
SAUDI_AIRPORTS = {
    "RUH", "JED", "DMM", "MED", "AHB", "TUU", "TIF", "GIZ", "ELQ", "HAS",
    "AJF", "RAE", "YNB", "EAM", "BHH", "ABT", "DWD", "AQI", "WAE", "URY",
    "HOF", "GYE", "KMX", "SHW", "AKH", "NUM", "RSI", "NEO",
}

# A practical set of passenger airports used to establish territorial scope.
# Carrier-only matches are intentionally not enough: route information is
# required before the app presents EU/UK rules as applicable.
EU_EEA_CH_AIRPORTS = {
    # Austria, Belgium, Bulgaria, Croatia, Cyprus, Czechia, Denmark, Estonia
    "VIE", "SZG", "INN", "GRZ", "BRU", "CRL", "SOF", "VAR", "BOJ", "ZAG",
    "SPU", "DBV", "LCA", "PFO", "PRG", "BRQ", "CPH", "BLL", "AAL", "TLL",
    # Finland, France, Germany, Greece
    "HEL", "RVN", "OUL", "CDG", "ORY", "NCE", "LYS", "MRS", "TLS", "BOD",
    "NTE", "SXB", "FRA", "MUC", "BER", "DUS", "HAM", "CGN", "STR", "HAJ",
    "NUE", "ATH", "SKG", "HER", "RHO", "CFU", "CHQ", "JTR", "KGS",
    # Hungary, Ireland, Italy, Latvia, Lithuania, Luxembourg, Malta
    "BUD", "DUB", "ORK", "SNN", "FCO", "MXP", "LIN", "BGY", "VCE", "NAP",
    "BLQ", "PSA", "CTA", "PMO", "TRN", "BRI", "RIX", "VNO", "KUN", "LUX",
    "MLA",
    # Netherlands, Poland, Portugal, Romania, Slovakia, Slovenia
    "AMS", "EIN", "RTM", "WAW", "KRK", "GDN", "WRO", "KTW", "LIS", "OPO",
    "FAO", "FNC", "OTP", "CLJ", "TSR", "IAS", "BTS", "LJU",
    # Spain, Sweden, Iceland, Norway, Switzerland
    "MAD", "BCN", "PMI", "AGP", "ALC", "VLC", "SVQ", "BIO", "TFS", "LPA",
    "ARN", "GOT", "MMX", "KEF", "OSL", "BGO", "TRD", "SVG", "TOS", "ZRH",
    "GVA", "BSL",
}

UK_AIRPORTS = {
    "LHR", "LGW", "STN", "LTN", "LCY", "SEN", "MAN", "BHX", "EDI", "GLA",
    "BRS", "NCL", "LPL", "BFS", "BHD", "ABZ", "EMA", "CWL", "SOU", "LBA",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19] if "%S" in fmt else
                                     value[:16] if "%H" in fmt else value[:10], fmt)
        except ValueError:
            continue
    return None


def effective(flight: dict, key: str):
    """Return a field with a user correction taking precedence."""
    overrides = flight.get("overrides") or {}
    value = overrides.get(key)
    return value if value not in (None, "") else flight.get(key)


def arrival_delay_hours(flight: dict) -> float | None:
    """Best available estimate of arrival delay, in hours."""
    scheduled = _parse_dt(effective(flight, "arrival"))
    actual = (_parse_dt(effective(flight, "actual_arrival"))
              or _parse_dt(flight.get("new_arrival")))
    if scheduled and actual:
        # A new time shortly after midnight is commonly emitted without the
        # next-day date. Treat a large negative delta as crossing midnight.
        delta = (actual - scheduled).total_seconds() / 3600
        if delta < -12:
            delta += 24
        return round(delta, 1)
    if flight.get("delay_hours") is not None:
        try:
            return float(flight["delay_hours"])
        except (TypeError, ValueError):
            return None
    return None


def _scopes(flight: dict) -> tuple[bool, bool, bool]:
    airline = flight.get("airline_code")
    origin = effective(flight, "origin")
    destination = effective(flight, "destination")
    gaca = origin in SAUDI_AIRPORTS or (
        destination in SAUDI_AIRPORTS and airline in SAUDI_CARRIERS)
    eu = origin in EU_EEA_CH_AIRPORTS or (
        destination in EU_EEA_CH_AIRPORTS and airline in EU_EEA_CARRIERS)
    uk = origin in UK_AIRPORTS or (
        destination in UK_AIRPORTS
        and airline in (UK_CARRIERS | EU_EEA_CARRIERS))
    return gaca, eu, uk


def _stronger(current: str, candidate: str) -> str:
    order = {UNKNOWN: 0, NOT_ELIGIBLE: 1, POSSIBLY: 2, ELIGIBLE: 3}
    return candidate if order[candidate] > order[current] else current


def assess(flight: dict) -> dict:
    """Return an explainable, conservative rights assessment.

    The return shape is stable for templates and callers and includes official
    sources so a passenger can verify the guidance before submitting a claim.
    """
    reasons: list[str] = []
    frameworks: list[str] = []
    sources: list[dict[str, str]] = []
    remedies: list[str] = []
    verdict = UNKNOWN

    origin = effective(flight, "origin")
    destination = effective(flight, "destination")
    delay = arrival_delay_hours(flight)
    cancelled = bool(effective(flight, "cancelled"))
    overrides = flight.get("overrides") or {}
    notice_days = overrides.get("cancellation_notice_days")
    accepted_alternative = overrides.get("accepted_alternative")
    denied_boarding = bool(overrides.get("denied_boarding"))
    gaca_applies, eu_applies, uk_applies = _scopes(flight)

    if gaca_applies:
        frameworks.append("GACA Passenger Rights Protection Regulation")
        sources.append({"label": "GACA passenger rights", "url": GACA_SOURCE})
    if eu_applies:
        frameworks.append("EU Regulation 261/2004")
        sources.append({"label": "EU air passenger rights", "url": EU_SOURCE})
    if uk_applies:
        frameworks.append("UK261 passenger rights")
        sources.append({"label": "UK CAA passenger rights", "url": UK_SOURCE})

    if not frameworks:
        frameworks.append("Airline conditions of carriage")
        if not origin or not destination:
            reasons.append(
                "The route is incomplete, so the app cannot safely determine "
                "which passenger-rights regime applies. Add the missing airport "
                "codes before relying on this result.")
        else:
            reasons.append(
                "The captured route and operating carrier do not establish "
                "GACA, EU261, or UK261 coverage. The airline's contract and the "
                "law of the departure country may still provide rights.")

    if cancelled and flight.get("cancellation_reason") == "not_airline_fault":
        verdict = NOT_ELIGIBLE
        reasons.append(
            "The email indicates that the booking—not the operated flight—was "
            "cancelled because payment or ticketing was not completed. That is "
            "not an airline disruption claim.")

    elif cancelled:
        if notice_days not in (None, ""):
            try:
                notice_days = float(notice_days)
            except (TypeError, ValueError):
                notice_days = None

        if accepted_alternative == "yes" and gaca_applies:
            reasons.append(
                "You indicated that you accepted an alternative flight. Under "
                "GACA, cancellation compensation no longer applies; the time "
                "difference is assessed under the delay rules instead.")
            if delay is None:
                verdict = POSSIBLY
                reasons.append(
                    "Enter the alternative flight's actual arrival time to "
                    "calculate any delay compensation.")
            elif delay > 6:
                verdict = ELIGIBLE
                remedies.append("150 Special Drawing Rights (GACA delay)")
                reasons.append(
                    f"The alternative arrived about {delay:g} hours late. GACA "
                    "provides 150 Special Drawing Rights for an arrival delay "
                    "of more than 6 hours.")
            elif delay >= 3:
                verdict = ELIGIBLE
                remedies.append("50 Special Drawing Rights (GACA delay)")
                reasons.append(
                    f"The alternative arrived about {delay:g} hours late. GACA "
                    "provides 50 Special Drawing Rights for a 3–6 hour arrival "
                    "delay.")
            else:
                verdict = NOT_ELIGIBLE
                reasons.append(
                    "The alternative-flight delay is below GACA's 3-hour cash "
                    "compensation threshold.")
        else:
            if gaca_applies:
                if accepted_alternative != "no":
                    verdict = _stronger(verdict, POSSIBLY)
                    reasons.append(
                        "For a GACA cancellation claim, confirm whether you "
                        "accepted an alternative flight. Cancellation percentages "
                        "apply when the contract is terminated; an accepted "
                        "alternative is handled as a delay.")
                elif notice_days is None:
                    verdict = _stronger(verdict, POSSIBLY)
                    reasons.append(
                        "Enter how many days before departure the airline notified "
                        "you. GACA cancellation compensation depends on that window.")
                elif notice_days >= 60:
                    verdict = _stronger(verdict, NOT_ELIGIBLE)
                    reasons.append(
                        "GACA's listed cancellation compensation bands begin within "
                        "60 days of departure. Refund or re-routing rights may remain.")
                else:
                    verdict = _stronger(verdict, ELIGIBLE)
                    if notice_days >= 14:
                        pct = 50
                    elif notice_days >= 1:
                        pct = 75
                    else:
                        pct = 150
                    remedies.append(
                        f"Refund plus {pct}% of the unused itinerary value (GACA)")
                    reasons.append(
                        f"With about {notice_days:g} days' notice and no accepted "
                        f"alternative, GACA provides a refund plus {pct}% of the "
                        "unused itinerary value, subject to the regulation's terms.")

            if eu_applies or uk_applies:
                if notice_days is None:
                    verdict = _stronger(verdict, POSSIBLY)
                    reasons.append(
                        "Enter the cancellation notice period. EU/UK cash "
                        "compensation generally depends on notice under 14 days, "
                        "re-routing times, and the cause.")
                elif notice_days < 14:
                    verdict = _stronger(verdict, POSSIBLY)
                    remedies.append("Potential fixed EU/UK compensation based on distance")
                    reasons.append(
                        "Notice was under 14 days. EU/UK compensation may be due, "
                        "but the alternative-flight timing and any extraordinary "
                        "circumstances must also be checked.")
                else:
                    verdict = _stronger(verdict, NOT_ELIGIBLE)
                    reasons.append(
                        "EU/UK fixed cancellation compensation is generally not due "
                        "when notice was given at least 14 days before departure.")

    elif denied_boarding:
        if gaca_applies:
            verdict = _stronger(verdict, POSSIBLY)
            remedies.append("Up to 200% of the unused itinerary value (GACA)")
            reasons.append(
                "Involuntary denied boarding can create a GACA claim. The exact "
                "remedy depends on whether you terminated the journey or accepted "
                "a replacement flight and when it arrived.")
        if eu_applies or uk_applies:
            verdict = _stronger(verdict, ELIGIBLE)
            remedies.append("Fixed EU/UK compensation plus re-routing or refund")
            reasons.append(
                "Involuntary denied boarding for overbooking or operational "
                "reasons normally triggers compensation and a choice of refund "
                "or re-routing, provided you checked in on time with valid documents.")
        if not (gaca_applies or eu_applies or uk_applies):
            verdict = POSSIBLY
            reasons.append(
                "Denied boarding was recorded, but the route is not complete "
                "enough to identify the governing compensation rules.")

    elif delay is not None:
        # A parser-extracted "delay by N hours" can refer to departure. Official
        # cash thresholds use arrival, so downgrade certainty until arrival time
        # is actually present or manually entered.
        has_arrival_evidence = bool(
            effective(flight, "actual_arrival") or flight.get("new_arrival"))
        candidate = ELIGIBLE if has_arrival_evidence else POSSIBLY

        if gaca_applies and delay > 6:
            verdict = _stronger(verdict, candidate)
            remedies.append("150 Special Drawing Rights (GACA)")
            reasons.append(
                f"Detected arrival delay: about {delay:g} hours. GACA provides "
                "150 Special Drawing Rights when arrival is more than 6 hours late.")
        elif gaca_applies and delay >= 3:
            verdict = _stronger(verdict, candidate)
            remedies.append("50 Special Drawing Rights (GACA)")
            reasons.append(
                f"Detected arrival delay: about {delay:g} hours. GACA provides "
                "50 Special Drawing Rights for an arrival delay from 3 to 6 hours.")

        if eu_applies and delay >= 3:
            verdict = _stronger(verdict, candidate)
            remedies.append("EUR 250–600 depending on route distance")
            reasons.append(
                f"Detected arrival delay: about {delay:g} hours. EU rules may "
                "provide fixed compensation after a 3-hour arrival delay unless "
                "the carrier proves extraordinary circumstances.")

        if uk_applies and delay >= 3:
            verdict = _stronger(verdict, candidate)
            remedies.append("GBP 220–520 depending on route distance and delay")
            reasons.append(
                f"Detected arrival delay: about {delay:g} hours. UK261 may "
                "provide GBP 220–520 after a 3-hour arrival delay, depending "
                "on distance and (for long-haul flights) the delay length, "
                "unless the carrier proves extraordinary circumstances.")

        if verdict == UNKNOWN:
            verdict = NOT_ELIGIBLE
            if gaca_applies and delay >= 1:
                reasons.append(
                    f"The delay is about {delay:g} hours—below GACA's 3-hour cash "
                    "threshold. Care is still due after 1 hour (drinks), 3 hours "
                    "(a meal), and 6 hours (hotel and transport when required).")
            else:
                reasons.append(
                    f"The detected delay of about {delay:g} hours is below the "
                    "cash-compensation threshold for the identified rules.")
        elif not has_arrival_evidence:
            reasons.append(
                "The delay duration came from an email notice and may describe "
                "departure rather than arrival. Enter the actual arrival time to "
                "turn this into a stronger assessment.")

    else:
        departure = (_parse_dt(effective(flight, "departure"))
                     or _parse_dt(effective(flight, "flight_date")))
        if departure and departure > datetime.now():
            reasons.append(
                "This appears to be an upcoming flight. Rescan after travel if "
                "the airline sends a delay or cancellation notice.")
        elif not origin or not destination or not effective(flight, "flight_date"):
            reasons.append(
                "There is not enough itinerary data to check this flight. Reprocess "
                "the stored emails or add the missing route and timing details.")
        else:
            verdict = NOT_ELIGIBLE
            reasons.append(
                "No delay, cancellation, or denied-boarding evidence was found in "
                "the linked emails. If the flight arrived late, enter the actual "
                "arrival time and check again.")

    if verdict in (ELIGIBLE, POSSIBLY):
        reasons.append(
            "Final entitlement depends on evidence, the operating carrier, any "
            "replacement offered, and documented extraordinary circumstances. "
            "Verify the official source before filing.")

    if verdict == ELIGIBLE:
        next_step = "Prepare a claim and attach the source emails."
    elif verdict == POSSIBLY:
        next_step = "Review the missing facts before filing."
    elif verdict == UNKNOWN:
        next_step = "Complete the route and disruption details."
    else:
        next_step = "Keep the record; no cash claim is currently detected."

    return {
        "verdict": verdict,
        "label": VERDICT_LABELS[verdict],
        "reasons": reasons,
        "frameworks": frameworks,
        "delay_hours": delay,
        "remedies": list(dict.fromkeys(remedies)),
        "next_step": next_step,
        "sources": sources,
        "verified_on": "2026-07-13",
    }
