"""Airline registry: IATA codes, sender domains and official complaint sites."""

AIRLINES = {
    "SV": {
        "name": "Saudia",
        "icao": "SVA",
        "domains": ["saudia.com", "saudiairlines.com", "alfursan.saudia.com"],
        # Open the official production complaint route directly instead of
        # depending on the generic contact page's cascading redirect fields.
        "complaint_url": "https://www.saudia.com/en/forms/complaint-form",
    },
    "XY": {
        "name": "flynas",
        "icao": "KNE",
        "domains": ["flynas.com"],
        "complaint_url": "https://help.flynas.com/en",
    },
    "F3": {
        "name": "flyadeal",
        "icao": "FAD",
        "domains": ["flyadeal.com"],
        "complaint_url": "https://help.flyadeal.com/hc/en-us/requests/new",
    },
    "RX": {
        "name": "Riyadh Air",
        # Flight-status providers can still use the marketing number when an
        # ICAO callsign is unavailable; the official IATA designator is RX.
        "icao": "",
        "domains": ["riyadhair.com"],
        "complaint_url": "https://www.riyadhair.com/en/help/contact-us",
    },
    "EK": {
        "name": "Emirates",
        "icao": "UAE",
        "domains": ["emirates.com"],
        "complaint_url": "https://www.emirates.com/english/help/",
    },
    "EY": {
        "name": "Etihad Airways",
        "icao": "ETD",
        "domains": ["etihad.com", "etihad.ae"],
        "complaint_url": "https://www.etihad.com/en/help",
    },
    "QR": {
        "name": "Qatar Airways",
        "icao": "QTR",
        "domains": ["qatarairways.com", "qatarairways.com.qa"],
        "complaint_url": "https://www.qatarairways.com/en/contact-us.html",
    },
    "GF": {
        "name": "Gulf Air",
        "icao": "GFA",
        "domains": ["gulfair.com"],
        "complaint_url": "https://www.gulfair.com/contact-us",
    },
    "KU": {
        "name": "Kuwait Airways",
        "icao": "KAC",
        "domains": ["kuwaitairways.com"],
        "complaint_url": "https://www.kuwaitairways.com/en/contact-us",
    },
    "MS": {
        "name": "EgyptAir",
        "icao": "MSR",
        "domains": ["egyptair.com"],
        "complaint_url": "https://www.egyptair.com/en/about-egyptair/Pages/contact-us.aspx",
    },
    "RJ": {
        "name": "Royal Jordanian",
        "icao": "RJA",
        "domains": ["rj.com", "royaljordanian.com"],
        "complaint_url": "https://www.rj.com/en/contact-us",
    },
    "TK": {
        "name": "Turkish Airlines",
        "icao": "THY",
        "domains": ["turkishairlines.com", "thy.com"],
        "complaint_url": "https://www.turkishairlines.com/en-int/any-questions/customer-relations/",
    },
    "BA": {
        "name": "British Airways",
        "icao": "BAW",
        "domains": ["britishairways.com", "email.ba.com", "ba.com"],
        "complaint_url": "https://www.britishairways.com/travel/customer-relations-int/public/en_gb",
    },
    "LH": {
        "name": "Lufthansa",
        "icao": "DLH",
        "domains": ["lufthansa.com", "milesandmore.com"],
        "complaint_url": "https://www.lufthansa.com/de/en/help-and-contact",
    },
    "AF": {
        "name": "Air France",
        "icao": "AFR",
        "domains": ["airfrance.com", "airfrance.fr"],
        "complaint_url": "https://wwws.airfrance.fr/en/contact",
    },
    "KL": {
        "name": "KLM",
        "icao": "KLM",
        "domains": ["klm.com", "klm.nl"],
        "complaint_url": "https://www.klm.com/help",
    },
    "PC": {
        "name": "Pegasus Airlines",
        "icao": "PGT",
        "domains": ["flypgs.com", "pegasusairlines.com"],
        "complaint_url": "https://www.flypgs.com/en/contact-us",
    },
    "WY": {
        "name": "Oman Air",
        "icao": "OMA",
        "domains": ["omanair.com"],
        "complaint_url": "https://www.omanair.com/en/contact-us",
    },
    "FZ": {
        "name": "flydubai",
        "icao": "FDB",
        "domains": ["flydubai.com"],
        "complaint_url": "https://www.flydubai.com/en/contact-us",
    },
}

# Carriers whose home regulator is GACA (Saudi Arabia).
SAUDI_CARRIERS = {"SV", "XY", "F3", "RX"}

# Carriers in the registry that establish inbound EU/EEA or UK territorial
# coverage. Pegasus is Turkish and must not be treated as an EU carrier; BA is
# assessed under the separate UK261 regime.
EU_EEA_CARRIERS = {"LH", "AF", "KL"}
UK_CARRIERS = {"BA"}

# Backwards-compatible name for integrations that imported the old constant.
EU_CARRIERS = EU_EEA_CARRIERS

GACA = {
    "name": "General Authority of Civil Aviation (GACA)",
    "portal_url": "https://myeservices.gaca.gov.sa/eservices/eservice/details?detailsId=2642710",
    "phone": "1929",
    "note": "GACA airline complaints are escalations: file with the carrier "
            "first, then escalate after seven days without a response or when "
            "the carrier's proposed resolution is unsatisfactory.",
}


def airline_for_domain(domain: str):
    """Return (code, info) for a sender domain, or (None, None)."""
    domain = (domain or "").lower().strip()
    for code, info in AIRLINES.items():
        for d in info["domains"]:
            if domain == d or domain.endswith("." + d):
                return code, info
    return None, None


def airline_for_name(text: str):
    """Find an airline whose name is mentioned in free text."""
    lowered = (text or "").lower()
    for code, info in AIRLINES.items():
        if info["name"].lower() in lowered:
            return code, info
    return None, None


def all_domains():
    return [d for info in AIRLINES.values() for d in info["domains"]]
