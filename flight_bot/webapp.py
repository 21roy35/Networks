"""Flask web GUI: flight list -> flight detail with copyable fields,
compensation assessment and one-click complaint generation."""

import threading
from pathlib import Path

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   url_for)

from . import db
from .compensation import assess, effective
from .complaints import airline_complaint, gaca_complaint, send_email
from .mail_client import eta_text
from .pipeline import load_demo, rebuild_flights, scan_mailbox

_ACTIVE_PHASES = {"starting", "connecting", "searching", "fetching", "linking"}

# Progress of the (single) background mailbox scan, polled by the GUI.
_scan_progress: dict = {}
_scan_lock = threading.Lock()


def _scan_running() -> bool:
    return _scan_progress.get("phase") in _ACTIVE_PHASES


def _run_scan(config: dict):
    try:
        scan_mailbox(config, log=lambda *a, **k: None,
                     progress=_scan_progress)
    except SystemExit as exc:  # missing credentials etc.
        _scan_progress.update(phase="error", error=str(exc))
    except Exception as exc:
        _scan_progress.update(phase="error", error=f"Scan failed: {exc}")


_TEMPLATES = Path(__file__).resolve().parent / "templates"
_REQUIRED_TEMPLATES = ("base.html", "index.html", "flight.html",
                       "complaint.html", "scan.html", "emails.html")


def create_app(config: dict) -> Flask:
    missing = [t for t in _REQUIRED_TEMPLATES if not (_TEMPLATES / t).exists()]
    if missing:
        raise SystemExit(
            f"Template file(s) missing from {_TEMPLATES}: {', '.join(missing)}.\n"
            "Your copy of the code is incomplete or out of date. Inside the "
            "Networks folder run:\n"
            "    git pull\n"
            "    git checkout -- flight_bot/templates\n"
            "then start the app again.")

    app = Flask(__name__, template_folder=str(_TEMPLATES))
    app.secret_key = "flight-bot-local-gui"  # local single-user app
    db.init_db()

    @app.route("/")
    def index():
        # Report the outcome of a finished background scan exactly once.
        phase = _scan_progress.get("phase")
        if phase in ("done", "error") and not _scan_progress.get("reported"):
            _scan_progress["reported"] = True
            if phase == "done":
                flash(f"Mailbox scanned — {_scan_progress.get('flights')} "
                      "flight(s) linked.")
            else:
                flash(_scan_progress.get("error") or "Scan failed.")
        flights = db.list_flights()
        for flight in flights:
            flight["assessment"] = assess(flight)
        return render_template("index.html", flights=flights)

    @app.route("/flight/<int:flight_id>")
    def flight_detail(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            flash("Flight not found.")
            return redirect(url_for("index"))
        fields = _copyable_fields(flight)
        return render_template("flight.html", flight=flight, fields=fields,
                               assessment=assess(flight))

    @app.route("/flight/<int:flight_id>/override", methods=["POST"])
    def flight_override(flight_id):
        for key in ("actual_arrival", "cancellation_notice_days",
                    "denied_boarding"):
            value = request.form.get(key, "").strip()
            if key == "denied_boarding":
                value = bool(request.form.get(key))
            elif key == "cancellation_notice_days" and value:
                try:
                    value = float(value)
                except ValueError:
                    value = ""
            db.set_override(flight_id, key, value or None)
        flash("Saved — eligibility re-checked with your corrections.")
        return redirect(url_for("flight_detail", flight_id=flight_id))

    @app.route("/flight/<int:flight_id>/complaint/gaca")
    def complaint_gaca(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            return redirect(url_for("index"))
        letter = gaca_complaint(flight, config["user"])
        return render_template("complaint.html", flight=flight, letter=letter,
                               kind="gaca")

    @app.route("/flight/<int:flight_id>/complaint/airline")
    def complaint_airline(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            return redirect(url_for("index"))
        letter = airline_complaint(flight, config["user"])
        return render_template("complaint.html", flight=flight, letter=letter,
                               kind="airline")

    @app.route("/flight/<int:flight_id>/complaint/airline/send", methods=["POST"])
    def complaint_airline_send(flight_id):
        flight = db.get_flight(flight_id)
        if not flight:
            return redirect(url_for("index"))
        letter = airline_complaint(flight, config["user"])
        to_addr = request.form.get("to") or letter["to"]
        if not to_addr:
            flash("No complaint email address known for this airline — "
                  "copy the letter and use the airline's web form instead.")
        else:
            try:
                flash(send_email(config, to_addr, letter["subject"],
                                 letter["body"]))
            except Exception as exc:  # surface SMTP problems in the GUI
                flash(f"Sending failed: {exc}")
        return redirect(url_for("complaint_airline", flight_id=flight_id))

    @app.route("/scan", methods=["POST"])
    def scan():
        with _scan_lock:
            if _scan_running():
                return redirect(url_for("scan_status"))
            _scan_progress.clear()
            _scan_progress.update(phase="starting", total=0, processed=0,
                                  kept=0)
            threading.Thread(target=_run_scan, args=(config,),
                             daemon=True).start()
        return redirect(url_for("scan_status"))

    @app.route("/scan/status")
    def scan_status():
        if not _scan_progress:
            return redirect(url_for("index"))
        return render_template("scan.html")

    @app.route("/scan/progress.json")
    def scan_progress():
        p = _scan_progress
        total, processed = p.get("total") or 0, p.get("processed") or 0
        return jsonify({
            "phase": p.get("phase", "idle"),
            "total": total,
            "processed": processed,
            "kept": p.get("kept", 0),
            "flights": p.get("flights"),
            "percent": round(100 * processed / total) if total else 0,
            "eta": eta_text(p),
            "error": p.get("error"),
        })

    @app.route("/demo", methods=["POST"])
    def demo():
        count = load_demo()
        flash(f"Demo data loaded — {count} flight(s) linked.")
        return redirect(url_for("index"))

    @app.route("/relink", methods=["POST"])
    def relink():
        count = rebuild_flights()
        flash(f"Emails re-linked into {count} flight(s).")
        return redirect(url_for("index"))

    @app.route("/emails")
    def emails():
        """Inspector: every stored email, what was extracted, and which
        flight it was linked to — for debugging linking problems."""
        flight_by_email = {}
        for flight in db.list_flights():
            full = db.get_flight(flight["id"])
            for e in full.get("emails", []):
                flight_by_email[e["db_id"]] = flight
        rows = db.all_emails()
        rows.sort(key=lambda e: e.get("date") or "", reverse=True)
        for e in rows:
            e["flight"] = flight_by_email.get(e["db_id"])
        return render_template("emails.html", emails=rows)

    return app


def _copyable_fields(flight: dict) -> list[tuple[str, str]]:
    """(label, value) pairs shown with copy buttons on the detail page."""
    overrides = flight.get("overrides") or {}
    route_from = " ".join(filter(None, [flight.get("origin_city"),
                                        f"({effective(flight, 'origin')})" if effective(flight, "origin") else None]))
    route_to = " ".join(filter(None, [flight.get("destination_city"),
                                      f"({effective(flight, 'destination')})" if effective(flight, "destination") else None]))
    pairs = [
        ("Airline", flight.get("airline_name")),
        ("Flight number", ", ".join(flight.get("flight_numbers") or [])),
        ("Flight date", flight.get("flight_date")),
        ("Booking reference (PNR)", flight.get("pnr")),
        ("E-ticket number", ", ".join(flight.get("ticket_numbers") or [])),
        ("Passenger", effective(flight, "passenger")),
        ("Origin", route_from),
        ("Destination", route_to),
        ("Scheduled departure", flight.get("departure")),
        ("Scheduled arrival", flight.get("arrival")),
        ("New departure (from delay notice)", flight.get("new_departure")),
        ("New/actual arrival", overrides.get("actual_arrival") or flight.get("new_arrival")),
        ("Boarding time", flight.get("boarding_time")),
        ("Gate", flight.get("gate")),
        ("Seat", flight.get("seat")),
        ("Cabin class", flight.get("cabin_class")),
        ("Ticket price", f"{flight.get('currency') or ''} {flight.get('amount')}".strip()
         if flight.get("amount") else None),
        ("Payment method", flight.get("payment_method")),
    ]
    return [(label, value) for label, value in pairs if value]
