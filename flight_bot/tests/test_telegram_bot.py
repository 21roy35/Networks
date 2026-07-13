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
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: None)
    bot.start()
    assert api.commands_registered is True


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

    def fake_start(payload, on_complete):
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

    def fake_start(payload, on_complete):
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
