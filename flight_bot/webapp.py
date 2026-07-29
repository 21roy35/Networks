"""Local Flask interface for inbox-derived flights and passenger claims."""

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from . import db
from .airlines import AIRLINES, airline_for_name
from .ai_assistant import ClaudeAssistant
from .case_strategy import recommend_case
from .captcha_solver import TwoCaptchaSolver
from .compensation import (ELIGIBLE, POSSIBLY, assess, effective)
from .complaints import (airline_complaint, complaint_payload, gaca_complaint,
                         missing_portal_fields)
from .flight_status import refresh_flight_status
from .mail_client import eta_text
from .config import (passenger_profile_key, save_passenger_profile,
                     save_user_profile)
from .pipeline import (load_demo, rebuild_flights, reparse_emails,
                       scan_mailbox)
from .portal_automation import (PortalResult, portal_job_status, set_ai_handler,
                                set_captcha_solver, set_category_handler,
                                start_portal_job)
from .telegram_bot import start_telegram
from .web_access import verify_web_token

_ACTIVE_PHASES = {"starting", "connecting", "searching", "fetching", "linking"}
_scan_progress: dict = {}
_scan_lock = threading.Lock()
_ai_profile_lock = threading.Lock()

_OTP_CONTEXT_RE = re.compile(
    r"(?:otp|one[ -]?time(?:\s+(?:password|code))?|verification\s+code|"
    r"authentication\s+code|security\s+code|login\s+code|passcode|"
    r"temporary\s+(?:password|pin)|"
    r"رمز\s*(?:التحقق|التأكيد|الدخول|المصادقة|الأمان))",
    re.I,
)
_SHORT_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_REFERENCE_RE = re.compile(
    r"(?:(?:complaint|case|request)(?:\s+reference)?|reference)\s*"
    r"(?:number|no\.?|id)?\s*"
    r"(?:is\s*)?[:#-]?\s*([A-Z0-9][A-Z0-9_-]{3,79})",
    re.I,
)

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_REQUIRED_TEMPLATES = (
    "base.html", "index.html", "flight.html", "complaint.html",
    "portal_status.html", "profile.html", "scan.html", "emails.html",
    "gaca_cases.html",
)


def _scan_running() -> bool:
    return _scan_progress.get("phase") in _ACTIVE_PHASES


def _ascii_digits(value: str) -> str:
    output = []
    for char in value:
        if not char.isdigit():
            continue
        try:
            output.append(str(unicodedata.digit(char)))
        except (TypeError, ValueError):
            continue
    return "".join(output)


def _extract_sms_otp(body: str) -> str:
    if not _OTP_CONTEXT_RE.search(body):
        return ""
    ranked = []
    for match in _SHORT_CODE_RE.finditer(body):
        candidate = _ascii_digits(match.group(1))
        context = body[max(0, match.start() - 80):match.end() + 80]
        score = (100 if _OTP_CONTEXT_RE.search(context) else 0)
        score += 10 if len(candidate) == 4 else 0
        ranked.append((score, candidate))
    return max(ranked, default=(0, ""))[1]


def _extract_sms_reference(body: str, sender: str = "") -> str:
    # Saudia uses C_1234567. Do not treat HRSD/SAMA "C2607..." as airline refs.
    match = re.search(r"(?<![A-Z0-9])C[_-](\d{6,})(?!\d)", body, re.I)
    if match:
        return f"C_{match.group(1)}"
    # GACA CARE currently sends public complaint IDs such as C076100 without
    # an underscore. Restrict this shape to GACA context so unrelated
    # government/customer-service IDs are not attached to aviation cases.
    gaca_context = bool(re.search(
        r"\bGACA\b|هيئة\s+الطيران", f"{sender}\n{body}", re.I))
    if gaca_context:
        match = re.search(r"(?<![A-Z0-9])(C\d{6,})(?!\d)", body, re.I)
        if match:
            return match.group(1).upper()
    match = _REFERENCE_RE.search(body)
    if not match:
        return ""
    candidate = match.group(1).upper()
    if re.fullmatch(r"C\d{6,}", candidate) and not gaca_context:
        return ""
    return candidate


def _normalize_sms_sender_body(sender: str, body: str) -> tuple[str, str]:
    sender = (sender or "").strip()
    body = (body or "").strip()
    looks_like_body = (
        len(sender) > 80
        or "\n" in sender
        or "\r" in sender
        or bool(re.search(r"\b(?:GR|EC|C_)\s*[-:]?\s*\d{5,}", sender, re.I))
    )
    if looks_like_body:
        if not body or body == sender or sender in body or body in sender:
            body = body or sender.lstrip("\r\n")
            folded = sender[:40].casefold()
            if "saudia" in folded:
                sender = "Saudia"
            elif "gaca" in folded or "طيران" in folded:
                sender = "GACA"
            elif re.search(r"(?i)\bcst\b|citc|هيئة\s*الاتصالات", folded):
                sender = "CST"
            else:
                sender = ""
        else:
            sender = re.sub(r"[\r\n]+", " ", sender).strip()[:80]
    if sender and body and sender == body:
        sender = ""
    return sender, body


def _is_telecom_cst_sms(sender: str, body: str) -> bool:
    blob = f"{sender}\n{body}"
    if re.search(r"\bGR\s*[-:]?\s*\d{6,}\b", blob, re.I) and re.search(
            r"(?i)cst|citc|هيئة|بلاغ", blob):
        return True
    return bool(re.fullmatch(r"(?i)cst|citc", (sender or "").strip()))


def _is_aviation_sms(sender: str, body: str, reference: str = "") -> bool:
    """Keep the shared shortcut feed scoped to airlines and aviation claims."""
    blob = f"{sender}\n{body}"
    _, info = airline_for_name(blob)
    if info:
        return True
    folded = re.sub(r"\s+", " ", blob.casefold())
    if re.search(r"(?i)\bgaca\b|هيئة\s*الطيران|الطيران\s*المدني", blob):
        return True
    if re.search(
        r"\bflight\b|boarding\s*pass|airport|baggage|luggage|\bpnr\b|"
        r"guest\s+relations|comment\s+ref|رحلة|مطار|أمتعة|امتعة",
        folded,
        re.I,
    ):
        return True
    # Saudia's acknowledgement format is carrier-specific even when the
    # shortcut omits the sender label.
    return bool(re.fullmatch(r"(?i)C_\d{6,}", str(reference or "").strip()))


def _is_gaca_sms(sender: str, body: str) -> bool:
    blob = f"{sender}\n{body}"
    return bool(re.search(
        r"(?i)\bgaca\b|"
        r"الهيئة\s*العامة\s*للطيران\s*المدني|"
        r"هيئة\s*الطيران|الطيران\s*المدني",
        blob,
    ))


def _run_scan(config: dict):
    try:
        scan_mailbox(config, log=lambda *a, **k: None,
                     progress=_scan_progress)
    except SystemExit as exc:
        _scan_progress.update(phase="error", error=str(exc))
    except Exception as exc:
        _scan_progress.update(phase="error", error=f"Scan failed: {exc}")


def _parse_display_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _data_quality(flight: dict) -> dict:
    checks = [
        (20, "flight number", bool(effective(flight, "flight_number")
                                    or flight.get("flight_numbers"))),
        (20, "travel date", bool(effective(flight, "flight_date"))),
        (30, "route", bool(effective(flight, "origin")
                           and effective(flight, "destination"))),
        (20, "booking reference", bool(flight.get("pnr")
                                       or flight.get("ticket_numbers"))),
        (10, "schedule", bool(effective(flight, "departure")
                              or effective(flight, "arrival"))),
    ]
    score = sum(weight for weight, _label, present in checks if present)
    missing = [label for _weight, label, present in checks if not present]
    return {"score": score, "missing": missing,
            "label": "Strong" if score >= 80 else
                     "Usable" if score >= 60 else "Needs details"}


def _payment_card(value: str | None) -> str:
    """Return a safe, filterable card label containing brand and last four."""
    method = (value or "").strip()
    if not method:
        return ""
    brands = (
        (r"american\s+express|\bamex\b", "American Express"),
        (r"master\s*card", "Mastercard"),
        (r"\bvisa\b", "Visa"),
        (r"\bmada\b", "Mada"),
        (r"apple\s*pay", "Apple Pay"),
        (r"google\s*pay", "Google Pay"),
        (r"samsung\s*pay", "Samsung Pay"),
    )
    brand = next((label for pattern, label in brands
                  if re.search(pattern, method, re.I)), "")
    if not brand:
        brand = re.split(
            r"(?:\s+[•*xX]{2,}|\s+ending\s+(?:in\s+)?|\s+\d{4}\s*$)",
            method, maxsplit=1, flags=re.I)[0].strip(" -–—:·")
        if re.fullmatch(r"[\s•*xX\d\-–—]+", brand):
            brand = ""
    digits = re.findall(r"\d", method)
    last_four = "".join(digits[-4:]) if len(digits) >= 4 else ""
    if last_four:
        return f"{brand or 'Card'} •••• {last_four}"
    return brand or method


def _flight_view(flight: dict) -> dict:
    item = dict(flight)
    item["assessment"] = assess(item)
    item["gaca_applicable"] = any(
        "gaca.gov.sa" in source.get("url", "")
        for source in item["assessment"].get("sources", []))
    item["quality"] = _data_quality(item)
    item["display_origin"] = effective(item, "origin") or "???"
    item["display_destination"] = effective(item, "destination") or "???"
    item["display_date"] = effective(item, "flight_date") or "Date unknown"
    item["display_flight_number"] = (
        effective(item, "flight_number")
        or ", ".join(item.get("flight_numbers") or [])
        or "Flight unknown")
    item["route"] = f"{item['display_origin']} → {item['display_destination']}"
    item["passenger_name"] = effective(item, "passenger") or ""
    payment_method = effective(item, "payment_method") or ""
    item["payment_card"] = _payment_card(payment_method)
    item["payment_method_display"] = item["payment_card"]

    when = (_parse_display_dt(effective(item, "departure"))
            or _parse_display_dt(effective(item, "flight_date")))
    if effective(item, "cancelled"):
        item["state"] = "cancelled"
        item["state_label"] = "Cancelled"
    elif item["assessment"].get("delay_hours", 0) and (
            item["assessment"].get("delay_hours") or 0) > 0:
        item["state"] = "disrupted"
        item["state_label"] = "Disrupted"
    elif when and when > datetime.now():
        item["state"] = "upcoming"
        item["state_label"] = "Upcoming"
    elif when:
        item["state"] = "past"
        item["state_label"] = "Completed"
    else:
        item["state"] = "unknown"
        item["state_label"] = "Needs review"

    item["search_blob"] = " ".join(str(value) for value in (
        item.get("airline_name"), item.get("airline_code"),
        item["display_flight_number"], item["display_date"], item["route"],
        item.get("pnr"), item["passenger_name"], item["payment_method_display"],
        item["assessment"]["label"], item["state_label"],
    ) if value).lower()
    return item


def _filter_flights(flights: list[dict], query: str = "", status: str = "all",
                    passenger: str = "", card: str = "") -> tuple[list[dict], str]:
    """Apply the dashboard and text-export filters through one shared path."""
    allowed_statuses = {"all", "claims", "disrupted", "upcoming", "needs_details"}
    if status not in allowed_statuses:
        status = "all"
    filtered = flights
    if query:
        lowered = query.casefold()
        filtered = [flight for flight in filtered
                    if lowered in flight["search_blob"].casefold()]
    if passenger:
        wanted = passenger.casefold()
        filtered = [flight for flight in filtered
                    if flight["passenger_name"].casefold() == wanted]
    if card:
        wanted = card.casefold()
        filtered = [flight for flight in filtered
                    if flight["payment_card"].casefold() == wanted]
    if status == "claims":
        filtered = [flight for flight in filtered if
                    flight["assessment"]["verdict"] in (ELIGIBLE, POSSIBLY)]
    elif status == "disrupted":
        filtered = [flight for flight in filtered if
                    flight["state"] in ("cancelled", "disrupted")]
    elif status == "upcoming":
        filtered = [flight for flight in filtered if flight["state"] == "upcoming"]
    elif status == "needs_details":
        filtered = [flight for flight in filtered if flight["quality"]["score"] < 60]
    return filtered, status


def _flights_as_text(flights: list[dict], filters: dict) -> str:
    lines = [
        "FLIGHTDECK FILTERED FLIGHT EXPORT",
        f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Results: {len(flights)}",
    ]
    active = [f"{label}={value}" for label, value in (
        ("search", filters.get("query")), ("status", filters.get("status")),
        ("passenger", filters.get("passenger")), ("card", filters.get("card")),
    ) if value and value != "all"]
    lines.append("Filters: " + ("; ".join(active) if active else "none"))
    lines.append("")
    for index, flight in enumerate(flights, 1):
        assessment = flight["assessment"]
        price = (f"{flight.get('currency') or ''} {flight.get('amount') or ''}".strip())
        facts = [
            ("Passenger", flight["passenger_name"]),
            ("Airline", flight.get("airline_name") or flight.get("airline_code")),
            ("Flight", flight["display_flight_number"]),
            ("Date", flight["display_date"]), ("Route", flight["route"]),
            ("Status", flight["state_label"]), ("PNR", flight.get("pnr")),
            ("E-ticket", ", ".join(flight.get("ticket_numbers") or [])),
            ("Scheduled departure", effective(flight, "departure")),
            ("Scheduled arrival", effective(flight, "arrival")),
            ("New departure", flight.get("new_departure")),
            ("Actual/latest arrival", effective(flight, "actual_arrival")
             or flight.get("new_arrival")),
            ("Cabin", flight.get("cabin_class")), ("Seat", flight.get("seat")),
            ("Gate", flight.get("gate")), ("Boarding", flight.get("boarding_time")),
            ("Ticket price", price), ("Payment method", flight["payment_card"]),
            ("Source emails", flight.get("email_count")),
            ("Evidence types", ", ".join(flight.get("kind_labels") or [])),
            ("Rights check", assessment["label"]),
            ("Framework", "; ".join(assessment.get("frameworks") or [])),
            ("Potential remedy", "; ".join(assessment.get("remedies") or [])),
        ]
        lines.extend((f"{index}. {flight['display_flight_number']} · {flight['route']}",
                      "-" * 60))
        lines.extend(f"{label}: {value}" for label, value in facts
                     if value not in (None, "", []))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _page_number(name: str = "page") -> int:
    try:
        return max(1, int(request.args.get(name, "1")))
    except ValueError:
        return 1


def _paginate(items: list, page: int, per_page: int) -> tuple[list, dict]:
    total = len(items)
    pages = max(1, math.ceil(total / per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    return items[start:end], {
        "page": page, "pages": pages, "total": total,
        "from": start + 1 if total else 0, "to": end,
        "has_prev": page > 1, "has_next": page < pages,
    }


def _normalise_datetime(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError as exc:
        raise ValueError("Use a valid date and time.") from exc
    return parsed.strftime("%Y-%m-%d %H:%M")


def _latest_airline_submission(flight: dict) -> dict | None:
    for item in reversed(flight.get("complaints") or []):
        if item.get("kind") == "airline" and item.get("status") == "submitted":
            return item
    return None


def create_app(config: dict) -> Flask:
    missing = [name for name in _REQUIRED_TEMPLATES
               if not (_TEMPLATES / name).exists()]
    if missing:
        raise SystemExit(
            f"Template file(s) missing from {_TEMPLATES}: {', '.join(missing)}")

    app = Flask(__name__, template_folder=str(_TEMPLATES))
    web_settings = config.get("web") or {}
    access_secret = str(web_settings.get("access_secret") or "")
    app.secret_key = access_secret or secrets.token_hex(24)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = str(
        web_settings.get("public_base_url") or "").startswith("https://")
    app.permanent_session_lifetime = timedelta(
        days=max(1, int(web_settings.get("session_days", 30))))
    db.init_db()
    assistant = ClaudeAssistant(config)
    set_ai_handler(
        assistant.portal_decision if assistant.enabled else None,
        int(assistant.settings.get("max_portal_attempts", 3)))
    set_category_handler(
        getattr(assistant, "choose_complaint_category", None)
        if assistant.enabled else None)
    captcha = TwoCaptchaSolver(config)
    set_captcha_solver(
        captcha.solve_recaptcha if captcha.enabled else None)
    telegram = start_telegram(config)
    sms_secret = str((config.get("sms") or {}).get("ingest_secret") or "")

    @app.before_request
    def require_private_web_link():
        if request.path in {"/healthz", "/api/internal/sms"} or not access_secret:
            return None
        supplied = request.args.get("access", "")
        if supplied:
            max_age = max(1, int(web_settings.get(
                "link_expiry_minutes", 15))) * 60
            if not verify_web_token(access_secret, supplied, max_age):
                return Response("This private FlightDeck link is invalid or expired.",
                                status=403, content_type="text/plain")
            session.permanent = True
            session["flightdeck_access"] = True
            args = request.args.to_dict(flat=False)
            args.pop("access", None)
            target = request.path
            if args:
                target += "?" + urlencode(args, doseq=True)
            return redirect(target)
        if not session.get("flightdeck_access"):
            return Response(
                "Private FlightDeck access required. Send /web to the Telegram bot for a fresh link.",
                status=401, content_type="text/plain")
        return None

    def sms_sender_email(sender: str, body: str) -> str:
        """Map an SMS sender label to a trusted airline domain for matching."""
        blob = f"{sender} {body}"
        _, info = airline_for_name(blob)
        if not info:
            folded = re.sub(r"[^a-z0-9]", "", blob.casefold())
            for candidate, candidate_info in AIRLINES.items():
                aliases = {
                    re.sub(r"[^a-z0-9]", "", candidate.casefold()),
                    re.sub(r"[^a-z0-9]", "", candidate_info["name"].casefold()),
                    re.sub(r"[^a-z0-9]", "", candidate_info.get("icao", "").casefold()),
                }
                if any(alias and alias in folded for alias in aliases):
                    info = candidate_info
                    break
        if not info:
            body_folded = body.casefold()
            for complaint in db.list_complaints():
                reference = str(complaint.get("reference") or "").casefold()
                if reference and reference in body_folded:
                    flight = complaint.get("flight_data") or {}
                    info = AIRLINES.get(flight.get("airline_code"))
                    break
        domains = (info or {}).get("domains") or []
        return f"sms@{domains[0]}" if domains else "sms@unknown.invalid"

    def attach_sms_reference(reference: str, trusted_sender: str,
                             body: str, event_id: int) -> int | None:
        """Attach an acknowledgement reference to the newest matching filing."""
        reference = str(reference or "").strip()
        if not reference:
            return None
        complaints = db.list_complaints()
        if any(str(item.get("reference") or "").casefold()
               == reference.casefold() for item in complaints):
            return None
        if _is_gaca_sms(trusted_sender, body):
            eligible_ids = db.gaca_confirmation_unknown_complaint_ids()
            pending = [
                item for item in complaints
                if item.get("kind") == "gaca"
                and int(item.get("id") or 0) in eligible_ids
                and not item.get("reference")
            ]
            # Retain only the newest ambiguous attempt for each flight. Older
            # attempts are historical diagnostics, not separate claims.
            newest_by_flight = {}
            for item in pending:
                newest_by_flight.setdefault(item.get("flight_key"), item)
            pending = list(newest_by_flight.values())
            compact_body = re.sub(r"[^a-z0-9]", "", body.casefold())
            fact_matches = []
            for complaint in pending:
                flight = complaint.get("flight_data") or {}
                overrides = flight.get("overrides") or {}
                values = [
                    flight.get("pnr"),
                    flight.get("flight_number"),
                    overrides.get("flight_number"),
                ]
                values.extend(flight.get("flight_numbers") or [])
                values.extend(flight.get("ticket_numbers") or [])
                tokens = [
                    re.sub(r"[^a-z0-9]", "", str(value).casefold())
                    for value in values if value
                ]
                if any(
                    len(token) >= 4 and token in compact_body
                    for token in tokens
                ):
                    fact_matches.append(complaint)
            if len(fact_matches) == 1:
                pending = fact_matches
            if len(pending) != 1:
                return None
            complaint = pending[0]
            if not db.reconcile_portal_confirmation(
                int(complaint["id"]), reference
            ):
                return None
            db.mark_event_seen(f"reference-captured:{event_id}")
            return int(complaint["id"])
        domain = trusted_sender.rsplit("@", 1)[-1].casefold()
        pending = []
        for complaint in complaints:
            if (complaint.get("kind") != "airline"
                    or complaint.get("status") != "accepted_pending_reference"
                    or complaint.get("reference")):
                continue
            flight = complaint.get("flight_data") or {}
            info = AIRLINES.get(flight.get("airline_code"), {})
            domains = [str(value).casefold()
                       for value in info.get("domains") or []]
            if domains and not any(
                    domain == value or domain.endswith("." + value)
                    for value in domains):
                continue
            pending.append(complaint)
        if not pending:
            return None

        # Prefer explicit booking/flight facts when the carrier includes them.
        # Otherwise list_complaints() is newest-first, which mirrors the email
        # acknowledgement matcher for a just-submitted complaint.
        compact_body = re.sub(r"[^a-z0-9]", "", body.casefold())
        fact_matches = []
        for complaint in pending:
            flight = complaint.get("flight_data") or {}
            overrides = flight.get("overrides") or {}
            values = [flight.get("pnr"), flight.get("flight_number"),
                      overrides.get("flight_number")]
            values.extend(flight.get("flight_numbers") or [])
            values.extend(flight.get("ticket_numbers") or [])
            tokens = [re.sub(r"[^a-z0-9]", "", str(value).casefold())
                      for value in values if value]
            if any(len(token) >= 4 and token in compact_body for token in tokens):
                fact_matches.append(complaint)
        complaint = fact_matches[0] if len(fact_matches) == 1 else pending[0]
        db.finish_complaint(complaint["id"], "submitted", reference)
        db.mark_event_seen(f"reference-captured:{event_id}")
        return int(complaint["id"])

    @app.post("/api/internal/sms")
    def ingest_sms():
        supplied = request.headers.get("X-SMS-Secret", "")
        if not sms_secret or not hmac.compare_digest(supplied, sms_secret):
            abort(404)
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            return jsonify(ok=False, error="JSON object required"), 400
        body = str(value.get("text") or value.get("body") or "").strip()
        sender = str(value.get("sender") or "")
        sender, body = _normalize_sms_sender_body(sender, body)
        if not body:
            return jsonify(ok=False, error="SMS text is required"), 400
        if len(body) > 20000:
            return jsonify(ok=False, error="SMS text is too long"), 413
        if _is_telecom_cst_sms(sender, body):
            return jsonify(ok=True, ignored=True, reason="telecom-cst")
        received_at = str(value.get("received_at") or "")[:100]
        message_id = str(value.get("message_id") or "")[:250]
        fingerprint = message_id or hashlib.sha256(
            f"{sender.casefold()}\x1f{received_at}\x1f{body}".encode("utf-8")
        ).hexdigest()
        otp = _extract_sms_otp(body)
        reference = _extract_sms_reference(body, sender)
        if not _is_aviation_sms(sender, body, reference):
            return jsonify(ok=True, ignored=True, reason="non-aviation")
        looks_distillable = bool(
            _SHORT_CODE_RE.search(body)
            or re.search(
                r"otp|verification|passcode|complaint|case|request|reference|ticket|"
                r"gaca|saudia|comment\s*ref",
                body, re.I,
            )
        )
        if assistant.enabled and looks_distillable and (not otp or not reference):
            distilled = assistant.distill_sms(sender, body) or {}
            otp = otp or str(distilled.get("otp") or "")
            reference = reference or str(distilled.get("reference") or "")
        if otp:
            receipt = hmac.new(
                sms_secret.encode("utf-8"),
                f"{sender.casefold()}\x1f{otp}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            is_new = db.remember_otp_receipt(receipt)
            accept_code = getattr(
                telegram, "accept_verification_code", None) if telegram else None
            consumed = bool(
                accept_code and accept_code(otp, source="SMS shortcut"))
            if is_new and telegram:
                telegram.notify(
                    f"🔐 FlightDeck verification code from "
                    f"{sender or 'unknown sender'}: {otp}\n"
                    + ("It was entered into the active portal automatically. "
                       if consumed else "")
                    + "The SMS body was not stored."
                )
            return jsonify(
                ok=True, ignored=True, reason="otp", otp=otp,
                duplicate=not is_new, consumed=consumed,
            )
        sms_id, created = db.save_sms_message({
            "fingerprint": fingerprint, "sender": sender,
            "received_at": received_at, "body": body,
            "source": str(value.get("source") or "telecombot-shortcut")[:100],
        })
        if not created:
            return jsonify(ok=True, duplicate=True)
        trusted_sender = sms_sender_email(sender, body)
        event_id = db.save_mail_event({
            "message_id": f"<sms-{fingerprint}@flightdeck.local>",
            "subject": (
                f"SMS from {sender or 'airline'}"
                + (f" · Complaint reference: {reference}" if reference else "")
            ),
            "sender": trusted_sender,
            "date": received_at or datetime.now().isoformat(),
            "body": body,
        })
        db.link_sms_mail_event(sms_id, event_id)
        attached_complaint_id = attach_sms_reference(
            reference, trusted_sender, body, event_id)
        if telegram:
            if attached_complaint_id:
                telegram.notify(
                    f"Captured aviation complaint reference {reference} from "
                    f"SMS and attached it to complaint #{attached_complaint_id}.")
            else:
                telegram.check_complaint_responses()
            telegram.notify(
                f"📱 FlightDeck received an airline SMS from {sender or 'unknown sender'} "
                "and checked it against active complaints.")
        return jsonify(
            ok=True, duplicate=False, mail_event_id=event_id,
            reference=reference, attached_complaint_id=attached_complaint_id,
        )

    @app.context_processor
    def template_context():
        return {"current_year": date.today().year,
                "scan_running": _scan_running()}

    @app.route("/")
    def index():
        phase = _scan_progress.get("phase")
        if phase in ("done", "error") and not _scan_progress.get("reported"):
            _scan_progress["reported"] = True
            if phase == "done":
                flash(f"Mailbox scan complete: {_scan_progress.get('flights', 0)} "
                      "flights linked.")
            else:
                flash(_scan_progress.get("error") or "The mailbox scan failed.")

        all_flights = [_flight_view(flight) for flight in db.list_flights()]
        summary = {
            "flights": len(all_flights),
            "claims": sum(f["assessment"]["verdict"] in (ELIGIBLE, POSSIBLY)
                          for f in all_flights),
            "disruptions": sum(f["state"] in ("cancelled", "disrupted")
                               for f in all_flights),
            "needs_details": sum(f["quality"]["score"] < 60
                                 for f in all_flights),
            **db.counts(),
        }

        query = request.args.get("q", "").strip()
        status = request.args.get("status", "all")
        passenger = request.args.get("passenger", "").strip()
        card = request.args.get("card", "").strip()
        passenger_options = sorted({flight["passenger_name"] for flight in all_flights
                                    if flight["passenger_name"]}, key=str.casefold)
        card_options = sorted({flight["payment_card"] for flight in all_flights
                               if flight["payment_card"]}, key=str.casefold)
        filtered, status = _filter_flights(
            all_flights, query=query, status=status, passenger=passenger, card=card)

        flights, pagination = _paginate(filtered, _page_number(), 30)
        config_ready = bool(config.get("imap", {}).get("user") and
                            config.get("imap", {}).get("password"))
        return render_template(
            "index.html", flights=flights, summary=summary,
            pagination=pagination, query=query, status=status,
            passenger=passenger, card=card, passenger_options=passenger_options,
            card_options=card_options, config_ready=config_ready)

    @app.route("/flights/export.txt")
    def export_flights_txt():
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "all")
        passenger = request.args.get("passenger", "").strip()
        card = request.args.get("card", "").strip()
        flights, status = _filter_flights(
            [_flight_view(flight) for flight in db.list_flights()],
            query=query, status=status, passenger=passenger, card=card)
        body = _flights_as_text(flights, {
            "query": query, "status": status, "passenger": passenger, "card": card})
        response = Response(body, content_type="text/plain; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="flightdeck-filtered-{date.today().isoformat()}.txt"')
        return response

    @app.route("/settings/profile", methods=["GET", "POST"])
    def profile_settings():
        next_url = request.values.get("next", "").strip()
        passenger_name = request.values.get("passenger", "").strip()
        if not (next_url.startswith("/") and not next_url.startswith("//")):
            next_url = url_for("index")
        family_mode = bool(passenger_name)
        if request.method == "POST":
            fields = (
                "first_name", "middle_name", "last_name", "email", "phone",
                "national_id", "title", "gender", "nationality", "country_code",
                "alfursan_id",
            )
            values = {field: request.form.get(field, "").strip()
                      for field in fields}
            values["full_name"] = " ".join(filter(None, (
                values["first_name"], values["middle_name"],
                values["last_name"])))
            required = (
                "first_name", "last_name", "email", "phone", "national_id",
                "title", "nationality", "country_code",
            )
            missing = [field.replace("_", " ") for field in required
                       if not values[field]]
            if missing:
                flash("Complete every profile field: " + ", ".join(missing) + ".")
            elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
                flash("Enter a valid contact email address.")
            elif not re.fullmatch(r"\+?[0-9 ()-]{7,20}", values["phone"]):
                flash("Enter a valid phone number.")
            elif not re.fullmatch(r"\+?\d{1,4}", values["country_code"]):
                flash("Use a country calling code such as +966.")
            else:
                if family_mode:
                    values["booking_name"] = passenger_name
                    key = passenger_profile_key(passenger_name)
                    config.setdefault("passengers", {})[key] = values
                    save_passenger_profile(passenger_name, values)
                    flash(f"Complaint profile saved for {passenger_name}.")
                else:
                    config["user"].update(values)
                    save_user_profile(values)
                    flash("Complaint profile saved. Future forms will be auto-filled.")
                return redirect(next_url)
        if family_mode:
            key = passenger_profile_key(passenger_name)
            profile = dict((config.get("passengers") or {}).get(key) or {})
            if not profile:
                parts = passenger_name.split()
                profile.update({
                    "booking_name": passenger_name,
                    "full_name": passenger_name,
                    "first_name": parts[0] if parts else "",
                    "middle_name": " ".join(parts[1:-1]) if len(parts) > 2 else "",
                    "last_name": parts[-1] if len(parts) > 1 else "",
                })
            suggestions = db.identity_suggestions(passenger_name)
            suggested_evidence = {}
            for field, value in suggestions["values"].items():
                if not profile.get(field):
                    profile[field] = value
                    suggested_evidence[field] = suggestions["evidence"].get(field, "")
            # The account owner's contact can receive updates when a ticket
            # has no labeled contact, but identity/loyalty data never falls
            # back across passengers.
            profile.setdefault("email", config["user"].get("email") or "")
            profile.setdefault("phone", config["user"].get("phone") or "")
            profile.setdefault("country_code",
                               config["user"].get("country_code") or "")
        else:
            profile = dict(config["user"])
            suggestions = db.identity_suggestions(
                profile.get("full_name") or " ".join(filter(None, (
                    profile.get("first_name"), profile.get("middle_name"),
                    profile.get("last_name"),
                ))))
            suggested_evidence = {}
            for field, value in suggestions["values"].items():
                if not profile.get(field):
                    profile[field] = value
                    suggested_evidence[field] = suggestions["evidence"].get(field, "")
        return render_template(
            "profile.html", profile=profile, next_url=next_url,
            passenger_name=passenger_name, family_mode=family_mode,
            suggested_evidence=suggested_evidence,
            profile_lookup_name=(passenger_name or profile.get("full_name") or ""),
            ai_profile_enabled=bool(
                assistant.enabled and
                assistant.settings.get("extract_profile_evidence", True)),
            ai_profile_name=assistant.name)

    @app.route("/settings/profile/ai-suggestions", methods=["POST"])
    def ai_profile_suggestions():
        passenger_name = request.form.get("passenger", "").strip()
        allowed_fields = {
            "title", "nationality", "email", "phone", "country_code",
            "national_id", "alfursan_id",
        }
        requested = {
            field for field in request.form.get("fields", "").split(",")
            if field in allowed_fields
        }
        if not passenger_name or not requested:
            return jsonify(status="nothing_needed", values={}, evidence={})
        if (not assistant.enabled
                or not assistant.settings.get("extract_profile_evidence", True)):
            return jsonify(status="disabled", values={}, evidence={})

        deterministic = db.identity_suggestions(passenger_name)
        blocked = set(deterministic.get("conflicts") or [])
        requested -= blocked
        requested -= set(deterministic.get("values") or {})
        if not requested:
            return jsonify(status="nothing_needed", values={}, evidence={})
        evidence = db.identity_evidence(passenger_name, limit=5)
        if not evidence:
            return jsonify(status="no_evidence", values={}, evidence={})
        evidence_hash = hashlib.sha256(json.dumps({
            "version": 1,
            "passenger": passenger_profile_key(passenger_name),
            "evidence": evidence,
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

        with _ai_profile_lock:
            result = db.get_ai_profile_cache(
                passenger_name, evidence_hash, assistant.model)
            cached = result is not None
            if result is None:
                result = assistant.extract_passenger_profile(
                    passenger_name, evidence)
                if result is None:
                    return jsonify(
                        status="unavailable", values={}, evidence={},
                        message=assistant.last_error or
                        "Anthropic profile extraction is temporarily unavailable.")
                db.save_ai_profile_cache(
                    passenger_name, evidence_hash, assistant.model, result)
        values = {
            field: value for field, value in (result.get("values") or {}).items()
            if field in requested and field not in blocked
        }
        labels = {
            field: source
            for field, source in (result.get("evidence") or {}).items()
            if field in values
        }
        return jsonify(
            status="found" if values else "checked", values=values,
            evidence=labels, cached=cached, assistant=assistant.name)

    @app.route("/flight/<int:flight_id>")
    def flight_detail(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        status_snapshot = (db.get_flight_status_snapshot(
            flight.get("flight_key") or "") or {
                "status": "not_checked", "label": "Not checked yet",
                "confidence": 0, "provider": "none", "sources": [],
            })
        complaint_ids = {item["id"] for item in flight.get("complaints") or []}
        responses = [item for item in db.complaint_response_details(50)
                     if item.get("complaint_id") in complaint_ids]
        strategy = recommend_case(
            flight, status_snapshot, flight.get("complaints") or [], responses,
            gaca_days=max(1, int((config.get("telegram") or {}).get(
                "gaca_auto_escalate_days", 7))))
        flight = _flight_view(flight)
        return render_template(
            "flight.html", flight=flight,
            field_groups=_copyable_field_groups(flight),
            assessment=flight["assessment"], live_status=status_snapshot,
            strategy=strategy)

    @app.route("/flight/<int:flight_id>/status/refresh", methods=["POST"])
    def flight_status_refresh(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        snapshot = refresh_flight_status(config, flight, force=True)
        flash("Flight status refreshed: " + str(
            snapshot.get("label") or snapshot.get("status") or "unknown"))
        return redirect(url_for("flight_detail", flight_id=flight_id))

    @app.route("/flight/<int:flight_id>/override", methods=["POST"])
    def flight_override(flight_id):
        if not db.get_flight(flight_id):
            abort(404)
        try:
            origin = request.form.get("origin", "").strip().upper()
            destination = request.form.get("destination", "").strip().upper()
            for value in (origin, destination):
                if value and not re.fullmatch(r"[A-Z]{3}", value):
                    raise ValueError("Airport codes must contain exactly 3 letters.")

            flight_date = request.form.get("flight_date", "").strip()
            if flight_date:
                date.fromisoformat(flight_date)
            notice = request.form.get("cancellation_notice_days", "").strip()
            notice_value = None
            if notice:
                notice_value = float(notice)
                if notice_value < 0:
                    raise ValueError("Cancellation notice cannot be negative.")
            accepted = request.form.get("accepted_alternative", "")
            if accepted not in ("", "yes", "no"):
                accepted = ""
            cancelled = request.form.get("cancelled", "")
            cancelled_value = ({"yes": True, "no": False}.get(cancelled)
                               if cancelled else None)
            values = {
                "flight_number": request.form.get("flight_number", "").strip().upper(),
                "flight_date": flight_date or None,
                "origin": origin or None,
                "destination": destination or None,
                "passenger": request.form.get("passenger", "").strip() or None,
                "payment_method": request.form.get("payment_method", "").strip() or None,
                "departure": _normalise_datetime(request.form.get("departure", "")),
                "arrival": _normalise_datetime(request.form.get("arrival", "")),
                "actual_arrival": _normalise_datetime(
                    request.form.get("actual_arrival", "")),
                "cancellation_notice_days": notice_value,
                "accepted_alternative": accepted or None,
                "cancelled": cancelled_value,
                "denied_boarding": bool(request.form.get("denied_boarding")),
            }
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("flight_detail", flight_id=flight_id))

        db.set_overrides(flight_id, values)
        flash("Corrections saved. The claim assessment has been recalculated.")
        return redirect(url_for("flight_detail", flight_id=flight_id))

    @app.route("/flight/<int:flight_id>/complaint/gaca")
    def complaint_gaca(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        prior = _latest_airline_submission(flight)
        reference = prior.get("reference") if prior else ""
        complaint_date = (prior.get("created_at") or "")[:10] if prior else ""
        incident = (prior.get("details") or "") if prior else ""
        missing = []
        profile_passenger = ""
        if reference:
            preview = complaint_payload(
                flight, config["user"], "gaca",
                incident if len(incident.strip()) >= 15 else
                "The airline did not resolve the passenger complaint.",
                reference or "", complaint_date,
                passenger_profiles=config.get("passengers") or {})
            missing = missing_portal_fields(preview)
            profile_passenger = preview.get("profile_passenger_name") or ""
        return render_template(
            "complaint.html", flight=_flight_view(flight),
            letter=gaca_complaint(flight, config["user"], incident,
                                  reference or "", complaint_date),
            kind="gaca", incident=incident, prior=prior,
            gaca_ready=bool(reference), missing=missing,
            profile_passenger=profile_passenger)

    @app.route("/flight/<int:flight_id>/complaint/airline")
    def complaint_airline(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        preview = complaint_payload(
            flight, config["user"], "airline",
            "Complaint details will be entered before submission.",
            passenger_profiles=config.get("passengers") or {})
        return render_template(
            "complaint.html", flight=_flight_view(flight),
            letter=airline_complaint(flight, config["user"]), kind="airline",
            incident="", prior=None, gaca_ready=False,
            missing=missing_portal_fields(preview),
            profile_passenger=preview.get("profile_passenger_name") or "")

    @app.route("/flight/<int:flight_id>/complaint/<kind>/submit",
               methods=["POST"])
    def complaint_submit(flight_id, kind):
        if kind not in ("gaca", "airline"):
            abort(404)
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        incident = request.form.get("incident", "").strip()
        prior = _latest_airline_submission(flight)
        reference = prior.get("reference") if prior else ""
        complaint_date = (prior.get("created_at") or "")[:10] if prior else ""
        ai_analysis = None
        if (len(incident) >= 15 and assistant.enabled
                and assistant.settings.get("analyze_incidents", True)):
            ai_analysis = assistant.analyze_incident(incident, flight)
        try:
            payload = complaint_payload(
                flight, config["user"], kind, incident,
                reference or "", complaint_date, ai_analysis=ai_analysis,
                passenger_profiles=config.get("passengers") or {})
        except ValueError as exc:
            flash(str(exc))
            endpoint = "complaint_gaca" if kind == "gaca" else "complaint_airline"
            return redirect(url_for(endpoint, flight_id=flight_id))
        missing = missing_portal_fields(payload)
        if missing:
            flash("Add the missing trip/profile data before submitting: "
                  + ", ".join(missing) + ".")
            endpoint = "complaint_gaca" if kind == "gaca" else "complaint_airline"
            claim_url = url_for(endpoint, flight_id=flight_id)
            if payload.get("passenger_profile_missing"):
                return redirect(url_for(
                    "profile_settings", next=claim_url,
                    passenger=payload.get("profile_passenger_name") or ""))
            return redirect(claim_url)

        flight_key = flight["flight_key"]
        subject = payload["subject"]
        complaint_id = db.begin_complaint(
            flight_key, kind, subject, incident,
            payload.get("attachments") or [],
            submitted_text=payload.get("description") or "",
            parent_complaint_id=(
                int(prior["id"]) if kind == "gaca" and prior else None),
            issue_summary=str(
                (ai_analysis or {}).get("summary") or incident),
            requested_resolution_summary=str(
                (ai_analysis or {}).get("requested_remedy") or ""),
        )
        if complaint_id is None:
            existing = db.active_complaint_for_flight(flight_key, kind)
            status = (existing or {}).get("status") or "filing"
            flash(
                f"This {kind.upper()} complaint is already {status.replace('_', ' ')}. "
                "FlightDeck will not submit it again.")
            return redirect(url_for("flight_detail", flight_id=flight_id))
        payload["portal_complaint_id"] = complaint_id

        def finish_record(status: str, reference_value: str | None = None):
            db.finish_complaint(
                complaint_id, status, reference_value,
                submitted_text=payload.get("description") or None,
                portal_category=(
                    payload.get("selected_complaint_category") or None))

        def record_result(result: PortalResult):
            if result.status == "submitted":
                finish_record("submitted", result.reference or None)
                if telegram:
                    suffix = (f" Reference: {result.reference}."
                              if result.reference else "")
                    telegram.notify(
                        f"{kind.upper()} complaint submitted through the official portal."
                        + suffix)
            elif result.status == "accepted_pending_reference":
                finish_record("accepted_pending_reference")
                if telegram:
                    telegram.notify(
                        f"{kind.upper()} was accepted without returning its "
                        "reference on the page. I will check email first; if the "
                        "reference is still missing after the mailbox scan, I will "
                        "ask for the SMS in Telegram. No duplicate will be filed.")
            elif result.status == "confirmation_unknown":
                finish_record("failed")
                if telegram:
                    telegram.notify(
                        f"{kind.upper()} returned no readable confirmation and "
                        "is recorded as failed, not submitted.")
            else:
                finish_record("failed")
                if telegram:
                    telegram.notify(
                        f"{kind.upper()} portal submission needs attention: {result.message}")

        if telegram:
            telegram.notify(
                f"Starting the {kind.upper()} complaint from your mobile request. "
                "I’ll send a Telegram screenshot if the official portal needs a CAPTCHA, OTP, required field, declaration, or final confirmation.")
        job_id = start_portal_job(
            payload, on_complete=record_result,
            on_update=telegram.portal_progress_handler() if telegram else None)
        return redirect(url_for("portal_status", job_id=job_id,
                                flight_id=flight_id))

    @app.route("/complaints/jobs/<job_id>")
    def portal_status(job_id):
        job = portal_job_status(job_id)
        if not job:
            abort(404)
        return render_template(
            "portal_status.html", job=job,
            flight_id=request.args.get("flight_id", type=int))

    @app.route("/complaints/jobs/<job_id>.json")
    def portal_status_json(job_id):
        job = portal_job_status(job_id)
        if not job:
            abort(404)
        return jsonify(job)

    @app.route("/scan", methods=["POST"])
    def scan():
        if not (config.get("imap", {}).get("user") and
                config.get("imap", {}).get("password")):
            flash("Add your mailbox address and app password to config.json first.")
            return redirect(url_for("index"))
        with _scan_lock:
            if _scan_running():
                return redirect(url_for("scan_status"))
            _scan_progress.clear()
            _scan_progress.update(phase="starting", total=0, processed=0,
                                  kept=0)
            threading.Thread(target=_run_scan, args=(config,), daemon=True).start()
        return redirect(url_for("scan_status"))

    @app.route("/scan/status")
    def scan_status():
        if not _scan_progress:
            return redirect(url_for("index"))
        return render_template("scan.html")

    @app.route("/scan/progress.json")
    def scan_progress():
        progress = dict(_scan_progress)
        total = progress.get("total") or 0
        processed = progress.get("processed") or 0
        return jsonify({
            "phase": progress.get("phase", "idle"),
            "total": total,
            "processed": processed,
            "kept": progress.get("kept", 0),
            "flights": progress.get("flights"),
            "percent": round(100 * processed / total) if total else 0,
            "eta": eta_text(progress),
            "error": progress.get("error"),
        })

    @app.route("/demo", methods=["POST"])
    def demo():
        count = load_demo(log=lambda *a, **k: None)
        flash(f"Demo loaded: {count} flights linked from sample emails.")
        return redirect(url_for("index"))

    @app.route("/reparse", methods=["POST"])
    def reparse():
        count = reparse_emails(log=lambda *a, **k: None)
        flash(f"Stored emails reprocessed with the latest parser: {count} flights linked.")
        return redirect(url_for("index"))

    @app.route("/relink", methods=["POST"])
    def relink():
        count = rebuild_flights(log=lambda *a, **k: None)
        flash(f"Existing extraction re-linked into {count} flights.")
        return redirect(url_for("index"))

    @app.route("/emails")
    def emails():
        rows = db.list_email_summaries()
        flight_by_email = db.email_flight_map()
        for email in rows:
            email["flight"] = flight_by_email.get(email["db_id"])

        query = request.args.get("q", "").strip()
        status = request.args.get("status", "all")
        linked_total = sum(bool(email["flight"]) for email in rows)
        if query:
            lowered = query.lower()
            rows = [email for email in rows if lowered in " ".join(str(value)
                    for value in (
                        email.get("subject"), email.get("sender"),
                        email.get("pnr"), email.get("flight_numbers"),
                        email.get("origin"), email.get("destination"),
                    ) if value).lower()]
        if status == "linked":
            rows = [email for email in rows if email["flight"]]
        elif status == "unlinked":
            rows = [email for email in rows if not email["flight"]]
        elif status != "all":
            status = "all"
        emails_page, pagination = _paginate(rows, _page_number(), 50)
        return render_template(
            "emails.html", emails=emails_page, pagination=pagination,
            query=query, status=status, email_total=db.counts()["emails"],
            linked_total=linked_total,
            unlinked_total=db.counts()["emails"] - linked_total)

    @app.route("/gaca-cases")
    def gaca_cases():
        rows = db.list_gaca_account_cases()
        query = request.args.get("q", "").strip()
        mapping = request.args.get("mapping", "all").strip()
        total = len(rows)
        mapped_total = sum(
            item.get("mapping_status") in {"mapped", "reconciled", "flight_only"}
            for item in rows
        )
        ambiguous_total = sum(
            item.get("mapping_status") == "ambiguous" for item in rows)
        if query:
            folded = query.casefold()
            rows = [
                item for item in rows
                if folded in " ".join(str(value or "") for value in (
                    item.get("reference"), item.get("status"),
                    item.get("airline"), item.get("airline_reference"),
                    item.get("flight_number"), item.get("flight_date"),
                    item.get("ticket_number"), item.get("pnr"),
                    item.get("passenger_name"), item.get("origin"),
                    item.get("destination"), item.get("category"),
                )).casefold()
            ]
        if mapping == "mapped":
            rows = [
                item for item in rows
                if item.get("mapping_status") in {
                    "mapped", "reconciled", "flight_only"}
            ]
        elif mapping == "review":
            rows = [
                item for item in rows
                if item.get("mapping_status") in {"ambiguous", "unmapped"}
            ]
        elif mapping != "all":
            mapping = "all"
        cases_page, pagination = _paginate(rows, _page_number(), 50)
        return render_template(
            "gaca_cases.html",
            cases=cases_page,
            pagination=pagination,
            query=query,
            mapping=mapping,
            total=total,
            mapped_total=mapped_total,
            ambiguous_total=ambiguous_total,
            sync=db.get_gaca_account_sync(),
        )

    @app.post("/gaca-cases/sync")
    def sync_gaca_cases():
        starter = getattr(telegram, "start_gaca_account_sync", None)
        if not starter:
            flash("Telegram must be connected before GACA Nafath sync can run.")
        elif starter(manual=True):
            flash(
                "GACA account sync started. Approve Nafath in Telegram if "
                "the saved session has expired.")
        else:
            flash("The GACA account sync is already running.")
        return redirect(url_for("gaca_cases"))

    @app.route("/healthz")
    def healthz():
        return jsonify(status="ok", **db.counts())

    return app


def _copyable_field_groups(flight: dict) -> list[dict]:
    overrides = flight.get("overrides") or {}
    route_from = " ".join(filter(None, [
        flight.get("origin_city"),
        f"({effective(flight, 'origin')})" if effective(flight, "origin") else None,
    ]))
    route_to = " ".join(filter(None, [
        flight.get("destination_city"),
        f"({effective(flight, 'destination')})" if effective(flight, "destination") else None,
    ]))

    groups = [
        ("Journey", [
            ("Airline", flight.get("airline_name") or flight.get("airline_code")),
            ("Flight number", effective(flight, "flight_number")
             or ", ".join(flight.get("flight_numbers") or [])),
            ("Travel date", effective(flight, "flight_date")),
            ("Origin", route_from), ("Destination", route_to),
            ("Scheduled departure", effective(flight, "departure")),
            ("Scheduled arrival", effective(flight, "arrival")),
            ("New departure", flight.get("new_departure")),
            ("Actual / latest arrival", overrides.get("actual_arrival")
             or flight.get("new_arrival")),
        ]),
        ("Booking", [
            ("Booking reference (PNR)", flight.get("pnr")),
            ("E-ticket number", ", ".join(flight.get("ticket_numbers") or [])),
            ("Passenger", effective(flight, "passenger")),
            ("Cabin class", flight.get("cabin_class")),
            ("Seat", flight.get("seat")), ("Gate", flight.get("gate")),
            ("Boarding time", flight.get("boarding_time")),
        ]),
        ("Payment", [
            ("Ticket price", f"{flight.get('currency') or ''} {flight.get('amount')}".strip()
             if flight.get("amount") else None),
            ("Payment method", _payment_card(effective(flight, "payment_method"))),
        ]),
    ]
    return [{"title": title,
             "fields": [(label, value) for label, value in fields if value]}
            for title, fields in groups if any(value for _label, value in fields)]
