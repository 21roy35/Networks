import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from flight_bot import db
from flight_bot.config import DEFAULTS
from flight_bot.pipeline import ingest, load_demo
from flight_bot.portal_automation import PortalResult, _annotate_grid, _parse_cells
from flight_bot.telegram_bot import TelegramCoordinator
from flight_bot import telegram_bot
from flight_bot.web_access import verify_web_token


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.deleted = []
        self.callbacks = []
        self.counter = 100
        self.commands_registered = False

    def set_commands(self):
        self.commands_registered = True

    def send_message(self, chat_id, text, reply_markup=None, force_reply=False):
        self.counter += 1
        record = {"message_id": self.counter, "chat": {"id": chat_id},
                  "text": text, "reply_markup": reply_markup,
                  "force_reply": force_reply}
        self.messages.append(record)
        return record

    def send_photo(self, chat_id, image, caption, reply_markup=None):
        self.counter += 1
        record = {"message_id": self.counter, "chat": {"id": chat_id},
                  "caption": caption, "image": image,
                  "reply_markup": reply_markup}
        self.photos.append(record)
        return record

    def answer_callback(self, query_id, text=""):
        self.callbacks.append((query_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), message_id))

    def download(self, file_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"telegram-photo")


@pytest.fixture()
def coordinator(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    monkeypatch.setattr(telegram_bot, "TELEGRAM_EVIDENCE_DIR",
                        tmp_path / "evidence")
    db.init_db()
    config = deepcopy(DEFAULTS)
    config["telegram"].update({
        "enabled": True, "bot_token": "test-token", "chat_id": "42",
        "complaint_debounce_seconds": 60, "mailbox_scan_minutes": 0,
    })
    config["user"].update({
        "full_name": "Test Passenger", "email": "p@example.com",
        "phone": "+966500000000", "national_id": "ID123",
        "title": "Mr", "nationality": "Saudi Arabian",
        "country_code": "+966",
    })
    api = FakeAPI()
    return TelegramCoordinator(config, api=api), api


def test_otp_is_relayed_and_deleted_after_use(coordinator):
    bot, api = coordinator
    result = {}

    def wait_for_code():
        result["value"] = bot.request_verification({
            "kind": "otp", "message": "Enter OTP", "image": b"shot"})

    thread = threading.Thread(target=wait_for_code)
    thread.start()
    deadline = time.time() + 2
    while not api.photos and time.time() < deadline:
        time.sleep(.01)
    bot.handle_update({"message": {
        "message_id": 77, "chat": {"id": 42}, "text": "123456"}})
    thread.join(2)
    assert result["value"] == "123456"
    assert api.deleted == [("42", 77)]


def test_start_registers_telegram_command_menu(coordinator, monkeypatch):
    bot, api = coordinator
    started = []
    monkeypatch.setattr(
        threading.Thread, "start", lambda thread: started.append(thread.name))
    bot.start()
    assert set(started) == {
        "telegram-commands", "telegram-updates", "telegram-monitor"}
    bot._register_commands()
    assert api.commands_registered is True


def test_complaint_reservation_blocks_duplicates_but_allows_failed_retry(
        coordinator):
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    first = db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken baggage")
    assert first is not None
    assert db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken baggage") is None

    db.finish_complaint(first, "needs_attention")
    retry = db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken baggage")
    assert retry is not None
    db.finish_complaint(retry, "confirmation_unknown")
    assert db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken baggage") is not None


def test_legacy_submitted_airline_row_without_reference_is_migrated_to_failed(
        coordinator):
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.add_complaint(
        flight["flight_key"], "airline", None, "Legacy UAT attempt",
        "submitted", details="No verifiable production reference.")

    db.init_db()

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["status"] == "failed"


def test_portal_progress_reports_stage_transitions_with_screenshots(coordinator):
    bot, api = coordinator
    relay = bot.portal_progress_handler()
    relay("opening", "Opening the official site.")
    relay("filling", "The site loaded.", b"loaded-screen")
    relay("filling", "The site loaded.", b"duplicate-screen")
    relay("submitting", "Form checks finished.", b"review-screen")
    relay("submitted", "Submission confirmed.")
    deadline = time.time() + 2
    while (len(api.messages) < 2 or len(api.photos) < 2) and time.time() < deadline:
        time.sleep(.01)

    assert "Doing now: opening the official website" in api.messages[0]["text"]
    assert api.photos[0]["image"] == b"loaded-screen"
    assert "Finished: opening the official website" in api.photos[0]["caption"]
    assert "Doing now: filling the complaint form" in api.photos[0]["caption"]
    assert len(api.photos) == 2


def test_due_flight_gets_one_telegram_survey(coordinator):
    bot, api = coordinator
    assert load_demo(log=lambda *_args, **_kwargs: None) == 3
    flight = db.list_flights()[0]
    arrival = (datetime.now() - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")
    db.set_overrides(flight["id"], {"arrival": arrival})
    bot.send_due_surveys(now=datetime.now())
    assert len(api.messages) == 1
    assert "How was" in api.messages[0]["text"]
    assert " from " in api.messages[0]["text"]
    assert " to " in api.messages[0]["text"]
    assert db.survey_for_flight(flight["flight_key"])["status"] == "asked"
    bot.send_due_surveys(now=datetime.now())
    assert len(api.messages) == 1


def test_post_flight_question_names_the_actual_family_passenger(coordinator):
    bot, _api = coordinator
    family_flight = {
        "flight_number": "SV1650", "origin": "JED", "destination": "AHB",
        "passenger": "Muhannad Alqahtani", "overrides": {},
    }
    owner_flight = {
        **family_flight, "passenger": "Test Passenger",
    }

    assert bot._post_flight_label(family_flight) == (
        "Muhannad Alqahtani's flight SV1650 from JED to AHB")
    assert bot._post_flight_label(owner_flight) == (
        "your flight SV1650 from JED to AHB")


def test_web_command_returns_short_lived_private_link(coordinator):
    bot, api = coordinator
    bot.config["web"].update({
        "public_base_url": "http://flightdeck.example:5000",
        "access_secret": "test-web-secret",
        "link_expiry_minutes": 15,
    })
    bot.handle_update({"message": {
        "message_id": 79, "chat": {"id": 42}, "text": "/web"}})
    button = api.messages[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Open FlightDeck"
    assert button["url"].startswith("http://flightdeck.example:5000/?access=")
    token = button["url"].split("?access=", 1)[1]
    assert verify_web_token("test-web-secret", token, 900)


def test_issue_text_and_photo_auto_file_to_official_portal(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], "42", 90, "awaiting_details")
    monkeypatch.setattr(telegram_bot, "missing_portal_fields", lambda _payload: [])

    captured = {}

    def fake_start(payload, on_complete, on_update=None):
        captured.update(payload)
        on_complete(PortalResult("submitted", "ok", "CAS-555000"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_start)
    bot.handle_update({"message": {
        "message_id": 91, "chat": {"id": 42},
        "reply_to_message": {"message_id": 90},
        "caption": "My seat was broken and the screen did not work.",
        "photo": [{"file_id": "small"}, {"file_id": "large"}],
    }})
    bot._finalize_intake(flight["flight_key"])
    assert captured["incident"].startswith("My seat was broken")
    assert len(captured["attachments"]) == 1
    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["reference"] == "CAS-555000"
    assert complaint["attachments"] == captured["attachments"]


def test_substantive_airline_response_offers_gaca_escalation(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-778899", details="Broken seat")
    db.save_mail_event({
        "message_id": "<reply@example>", "subject": "Case CAS-778899 resolved",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "We reviewed CAS-778899 and declined compensation. The case is closed.",
    })
    bot.check_complaint_responses()
    assert len(api.messages) == 1
    assert "responded" in api.messages[0]["text"]
    buttons = api.messages[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("escalate:")


def test_confirmation_email_recovers_missing_airline_reference(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    db.save_mail_event({
        "message_id": "<confirmation@example>",
        "subject": "Complaint reference number is CAS-44556677",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "Thank you. Your complaint was received.",
    })

    bot.check_complaint_responses()

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["reference"] == "CAS-44556677"
    assert complaint["status"] == "submitted"
    assert any("Captured the airline complaint reference" in item["text"]
               for item in api.messages)


def test_one_confirmation_reference_is_not_assigned_to_two_cases(coordinator):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Older complaint",
        "submitted", details="An older issue.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-1 day')")
    db.add_complaint(
        flight["flight_key"], "airline", None, "New screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    db.save_mail_event({
        "message_id": "<one-confirmation@example>",
        "subject": "Complaint reference number is CAS-99880011",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "Thank you. Your complaint was received.",
    })

    bot.check_complaint_responses()

    older, newer = db.complaints_for_flight(flight["flight_key"])
    assert older["reference"] is None
    assert newer["reference"] == "CAS-99880011"


def test_saudia_ticket_subject_recovers_missing_reference(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    db.save_mail_event({
        "message_id": "<saudia-ticket@example>",
        "subject": "Your Ticket\xa0C_2771234 is Registered with us",
        "sender": "CR-NORPLY@saudia.com", "date": datetime.now(),
        "body": "Thank you for contacting Saudia Guest Relations.",
    })

    bot.check_complaint_responses()

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["reference"] == "C_2771234"
    assert complaint["status"] == "submitted"
    assert any("C_2771234" in item["text"] for item in api.messages)


def test_telegram_pasted_sms_captures_only_reference_and_keeps_original_date(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    original = datetime.now() - timedelta(days=8)
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE flight_key = ?",
            (original.strftime("%Y-%m-%d %H:%M:%S"), flight["flight_key"]))

    bot.handle_update({"message": {
        "message_id": 301, "chat": {"id": 42},
        "text": ("SAUDIA: We received your comment. Your reference is "
                 "C_2774567. Keep this SMS for your records."),
    }})

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["status"] == "submitted"
    assert complaint["reference"] == "C_2774567"
    assert complaint["details"] == "The screen was broken."
    assert complaint["created_at"] == original.strftime("%Y-%m-%d %H:%M:%S")
    assert "Keep this SMS" not in str(complaint)
    assert any("stored only the reference" in item["text"] for item in api.messages)

    launched = []
    monkeypatch.setattr(
        bot, "_launch_gaca",
        lambda selected, incident_suffix="", automatic=False:
        launched.append((selected["id"], incident_suffix, automatic)) or True)
    bot.auto_escalate_due_complaints(now=datetime.now())
    assert launched and launched[0][0] == flight["id"]
    assert launched[0][2] is True


def test_bot_asks_once_for_pending_saudia_sms_reference(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-3 minutes') "
            "WHERE flight_key = ?", (flight["flight_key"],))

    bot.ask_for_pending_references()
    bot.ask_for_pending_references()

    assert len(api.messages) == 1
    assert api.messages[0]["force_reply"] is True
    assert "paste its text" in api.messages[0]["text"]
    assert "service-ticket number" in api.messages[0]["text"]


def test_contextual_saudia_ticket_number_is_normalized_to_case_reference(
        coordinator):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")

    bot.handle_update({"message": {
        "message_id": 303, "chat": {"id": 42},
        "text": "SAUDIA: Your Ticket 2777654 is Registered with us.",
    }})

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["status"] == "submitted"
    assert complaint["reference"] == "C_2777654"


def test_bot_waits_for_email_scan_window_before_asking_for_sms(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")

    bot.ask_for_pending_references(now=datetime.now())

    assert api.messages == []
    assert not db.event_seen("telegram-reference-requested:1")


def test_plain_ticket_number_is_not_mistaken_for_sms_reference(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")

    bot.handle_update({"message": {
        "message_id": 302, "chat": {"id": 42},
        "text": "My flight ticket number is 0652200120916.",
    }})

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["status"] == "accepted_pending_reference"
    assert complaint["reference"] is None
    assert api.messages[-1]["text"].startswith("Reply to a post-flight")


def test_e_ticket_subject_is_not_used_as_complaint_reference(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="The screen was broken.")
    db.save_mail_event({
        "message_id": "<eticket@example>",
        "subject": "Flight Confirmation ETicket Receipt",
        "sender": "info@saudia.com", "date": datetime.now(),
        "body": "Ticket number 0652200120916",
    })

    bot.check_complaint_responses()

    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert complaint["reference"] is None
    assert complaint["status"] == "accepted_pending_reference"
    assert api.messages == []


def test_ghala_interprets_matched_airline_response_before_escalation(coordinator):
    bot, api = coordinator

    class FakeGhala:
        enabled = True
        name = "Ghala-200"
        model = "claude-sonnet-5"
        settings = {"analyze_responses": True}

        def analyze_response(self, *_args):
            return {
                "summary": "The airline declined compensation.",
                "outcome": "declined", "amounts_or_deadlines": [],
                "recommendation": "escalate",
                "rationale": "The complaint was closed without a remedy.",
                "substantive": True,
            }

    bot.ai = FakeGhala()
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-667788", details="Broken seat")
    db.save_mail_event({
        "message_id": "<ai-reply@example>", "subject": "Case CAS-667788 update",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "We have completed our review of CAS-667788.",
    })
    bot.check_complaint_responses()
    assert len(api.messages) == 1
    assert "declined compensation" in api.messages[0]["text"]
    assert "Ghala-200 recommends: escalate" in api.messages[0]["text"]


def test_closed_case_ignores_later_airline_messages(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-111222", details="Broken seat")
    bot._handle_callback({
        "id": "callback-close", "data": f"close_case:{flight['id']}",
        "message": {"chat": {"id": 42}},
    })
    api.messages.clear()
    db.save_mail_event({
        "message_id": "<later-reply@example>",
        "subject": "Case CAS-111222 resolved",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "We reviewed CAS-111222 and declined compensation.",
    })
    bot.check_complaint_responses()
    assert api.messages == []


def test_gaca_callback_files_with_airline_reference(coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-333444", details="The seat was broken.",
        attachments=["seat.jpg"])
    monkeypatch.setattr(telegram_bot, "missing_portal_fields", lambda _payload: [])
    captured = {}

    def fake_start(payload, on_complete, on_update=None):
        captured.update(payload)
        on_complete(PortalResult("submitted", "ok", "GACA-98765"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_start)
    bot._handle_callback({
        "id": "callback-gaca", "data": f"escalate:{flight['id']}",
        "message": {"chat": {"id": 42}},
    })
    assert captured["kind"] == "gaca"
    assert captured["airline_reference"] == "CAS-333444"
    complaint = db.complaints_for_flight(flight["flight_key"])[-1]
    assert complaint["kind"] == "gaca"
    assert complaint["reference"] == "GACA-98765"


def test_seven_day_no_response_auto_escalates_once(coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Seat screen complaint",
        "submitted", reference="CAS-700001",
        details="The seat-back entertainment screen was broken.")
    submitted_at = datetime.now() - timedelta(days=8)
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE reference = ?",
            (submitted_at.strftime("%Y-%m-%d %H:%M:%S"), "CAS-700001"))
    airline_complaint = db.complaints_for_flight(flight["flight_key"])[0]
    monkeypatch.setattr(telegram_bot, "missing_portal_fields", lambda _payload: [])
    captured = []

    def fake_start(payload, on_complete, on_update=None):
        captured.append(payload)
        on_complete(PortalResult("submitted", "ok", "GACA-700001"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_start)
    bot.auto_escalate_due_complaints(now=datetime.now())
    bot.auto_escalate_due_complaints(now=datetime.now())

    assert len(captured) == 1
    assert captured[0]["kind"] == "gaca"
    assert "did not provide a substantive response" in captured[0]["incident"]
    assert db.event_seen(f"auto-gaca:{airline_complaint['id']}")
    complaints = db.complaints_for_flight(flight["flight_key"])
    assert [item["kind"] for item in complaints] == ["airline", "gaca"]
    assert complaints[-1]["reference"] == "GACA-700001"
    assert any("automatically escalating" in item["text"] for item in api.messages)


def test_auto_escalation_waits_and_skips_detected_response(
        coordinator, monkeypatch):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-700002", details="Broken screen")
    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    calls = []
    monkeypatch.setattr(bot, "_launch_gaca", lambda *_args, **_kwargs: calls.append(1))

    bot.auto_escalate_due_complaints(now=datetime.now() + timedelta(days=6))
    assert calls == []
    db.mark_event_seen(f"airline-responded:{complaint['id']}")
    bot.auto_escalate_due_complaints(now=datetime.now() + timedelta(days=8))
    assert calls == []


def test_due_escalation_waits_visibly_for_required_airline_reference(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Screen complaint",
        "accepted_pending_reference", details="Broken screen")
    complaint_id = db.complaints_for_flight(flight["flight_key"])[0]["id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE id = ?",
            ((datetime.now() - timedelta(days=8)).strftime(
                "%Y-%m-%d %H:%M:%S"), complaint_id))
    calls = []
    monkeypatch.setattr(
        bot, "_launch_gaca",
        lambda *_args, **_kwargs: calls.append(1) or True)

    bot.auto_escalate_due_complaints(now=datetime.now())
    bot.auto_escalate_due_complaints(now=datetime.now())

    assert calls == []
    assert sum("GACA requires" in item["text"] for item in api.messages) == 1
    assert db.event_seen(f"auto-gaca-waiting-reference:{complaint_id}")
    assert not db.event_seen(f"auto-gaca:{complaint_id}")

    db.finish_complaint(complaint_id, "submitted", "CAS-700003")
    bot.auto_escalate_due_complaints(now=datetime.now())
    assert calls == [1]
    assert db.event_seen(f"auto-gaca:{complaint_id}")


def test_every_candidate_mail_is_kept_for_response_matching(
        coordinator):
    raw = [{
        "message_id": "<non-flight-response>", "subject": "Your case was resolved",
        "sender": "care@saudia.com", "date": datetime.now(),
        "body": "No booking details appear in this customer-service response.",
    }]
    assert ingest(raw, log=lambda *_args, **_kwargs: None) == 0
    assert db.list_mail_events()[0]["message_id"] == "<non-flight-response>"


def test_grid_answers_are_numbered_for_telegram():
    from PIL import Image
    from io import BytesIO

    source = BytesIO()
    Image.new("RGB", (300, 300), "gray").save(source, format="PNG")
    annotated = _annotate_grid(source.getvalue(), 9)
    assert annotated.startswith(b"\x89PNG")
    assert _parse_cells("1, 4 and 9", 9) == [1, 4, 9]
