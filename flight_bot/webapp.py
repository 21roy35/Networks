"""Local Flask interface for inbox-derived flights and passenger claims."""

import math
import re
import secrets
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from . import db
from .ai_assistant import ClaudeAssistant
from .captcha_solver import TwoCaptchaSolver
from .compensation import (ELIGIBLE, POSSIBLY, assess, effective)
from .complaints import (airline_complaint, complaint_payload, gaca_complaint,
                         missing_portal_fields)
from .mail_client import eta_text
from .config import save_user_profile
from .pipeline import (load_demo, rebuild_flights, reparse_emails,
                       scan_mailbox)
from .portal_automation import (PortalResult, portal_job_status, set_ai_handler,
                                set_captcha_solver, start_portal_job)
from .telegram_bot import start_telegram
from .web_access import verify_web_token

_ACTIVE_PHASES = {"starting", "connecting", "searching", "fetching", "linking"}
_scan_progress: dict = {}
_scan_lock = threading.Lock()

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_REQUIRED_TEMPLATES = (
    "base.html", "index.html", "flight.html", "complaint.html",
    "portal_status.html", "profile.html", "scan.html", "emails.html",
)


def _scan_running() -> bool:
    return _scan_progress.get("phase") in _ACTIVE_PHASES


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
    captcha = TwoCaptchaSolver(config)
    set_captcha_solver(
        captcha.solve_recaptcha if captcha.enabled else None)
    telegram = start_telegram(config)

    @app.before_request
    def require_private_web_link():
        if request.path == "/healthz" or not access_secret:
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
        if not (next_url.startswith("/") and not next_url.startswith("//")):
            next_url = url_for("index")
        if request.method == "POST":
            fields = (
                "first_name", "middle_name", "last_name", "email", "phone",
                "national_id", "title", "nationality", "country_code",
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
                config["user"].update(values)
                save_user_profile(values)
                flash("Complaint profile saved. Future forms will be auto-filled.")
                return redirect(next_url)
        return render_template(
            "profile.html", profile=config["user"], next_url=next_url)

    @app.route("/flight/<int:flight_id>")
    def flight_detail(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        flight = _flight_view(flight)
        return render_template(
            "flight.html", flight=flight,
            field_groups=_copyable_field_groups(flight),
            assessment=flight["assessment"])

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
        return render_template(
            "complaint.html", flight=_flight_view(flight),
            letter=gaca_complaint(flight, config["user"], incident,
                                  reference or "", complaint_date),
            kind="gaca", incident=incident, prior=prior,
            gaca_ready=bool(reference), missing=[])

    @app.route("/flight/<int:flight_id>/complaint/airline")
    def complaint_airline(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            abort(404)
        preview = complaint_payload(
            flight, config["user"], "airline",
            "Complaint details will be entered before submission.")
        return render_template(
            "complaint.html", flight=_flight_view(flight),
            letter=airline_complaint(flight, config["user"]), kind="airline",
            incident="", prior=None, gaca_ready=False,
            missing=missing_portal_fields(preview))

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
                reference or "", complaint_date, ai_analysis=ai_analysis)
        except ValueError as exc:
            flash(str(exc))
            endpoint = "complaint_gaca" if kind == "gaca" else "complaint_airline"
            return redirect(url_for(endpoint, flight_id=flight_id))
        missing = missing_portal_fields(payload)
        if missing:
            flash("Add the missing trip/profile data before submitting: "
                  + ", ".join(missing) + ".")
            return redirect(url_for("flight_detail", flight_id=flight_id))

        flight_key = flight["flight_key"]
        subject = payload["subject"]
        complaint_id = db.begin_complaint(
            flight_key, kind, subject, incident,
            payload.get("attachments") or [])
        if complaint_id is None:
            existing = db.active_complaint_for_flight(flight_key, kind)
            status = (existing or {}).get("status") or "filing"
            flash(
                f"This {kind.upper()} complaint is already {status.replace('_', ' ')}. "
                "FlightDeck will not submit it again.")
            return redirect(url_for("flight_detail", flight_id=flight_id))

        def record_result(result: PortalResult):
            if result.status == "submitted":
                db.finish_complaint(
                    complaint_id, "submitted", result.reference or None)
                if telegram:
                    suffix = (f" Reference: {result.reference}."
                              if result.reference else "")
                    telegram.notify(
                        f"{kind.upper()} complaint submitted through the official portal."
                        + suffix)
            elif result.status == "confirmation_unknown":
                db.finish_complaint(complaint_id, "confirmation_unknown")
                if telegram:
                    telegram.notify(
                        f"{kind.upper()} was sent once without a readable "
                        "confirmation. FlightDeck will not submit it again.")
            else:
                db.finish_complaint(complaint_id, "needs_attention")
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
