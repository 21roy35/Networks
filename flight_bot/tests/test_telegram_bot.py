import json
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
import requests

from flight_bot import db
from flight_bot.config import DEFAULTS
from flight_bot.pipeline import ingest, load_demo
from flight_bot.portal_automation import PortalResult, _annotate_grid, _parse_cells
from flight_bot.gaca_account import GacaAccountSyncResult
from flight_bot.gaca_status import GacaCaseResult
from flight_bot.telegram_bot import TelegramAPI, TelegramCoordinator
from flight_bot import telegram_bot
from flight_bot.web_access import verify_web_token


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.deleted = []
        self.edits = []
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

    def edit_message_text(self, chat_id, message_id, text):
        record = {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "text": text,
        }
        self.edits.append(record)
        return record

    def answer_callback(self, query_id, text=""):
        self.callbacks.append((query_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), message_id))

    def download(self, file_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"telegram-photo")


def test_gaca_email_reference_matches_the_exact_pending_flight(monkeypatch):
    complaints = [
        {
            "id": 96,
            "kind": "gaca",
            "status": "accepted_pending_reference",
            "reference": None,
            "flight_data": {
                "pnr": "8GAKAN",
                "flight_number": "SV1674",
                "flight_numbers": ["SV1674"],
                "ticket_numbers": ["065-2200541313"],
            },
        },
        {
            "id": 98,
            "kind": "gaca",
            "status": "accepted_pending_reference",
            "reference": None,
            "flight_data": {
                "pnr": "7V5F9V",
                "flight_number": "SV1671",
                "flight_numbers": ["SV1671"],
                "ticket_numbers": ["065-2200278935"],
            },
        },
    ]
    reconciled = []
    seen = []
    monkeypatch.setattr(db, "list_complaints", lambda: complaints)
    monkeypatch.setattr(db, "event_seen", lambda _key: False)
    monkeypatch.setattr(
        db,
        "reconcile_portal_confirmation",
        lambda complaint_id, reference:
        reconciled.append((complaint_id, reference)) or True,
    )
    monkeypatch.setattr(db, "mark_event_seen", seen.append)

    count = telegram_bot.reconcile_gaca_mail_events([{
        "id": 501,
        "sender": "GACA Care <care@gaca.gov.sa>",
        "subject": "GACA complaint C076222 - SV1674",
        "body": "Booking 8GAKAN has been registered.",
    }])

    assert count == 1
    assert reconciled == [(96, "C076222")]
    assert seen == ["gaca-reference-captured:501"]


def test_saudia_confirmation_prefers_case_reference_over_emd_voucher():
    reference = telegram_bot._airline_confirmation_reference(
        "EMD refundable Compensation request for case C_2737927 has been completed",
        "Voucher no. 0654228947655 Amount in SAR 752.45",
    )

    assert reference == "C_2737927"


def test_riyadh_air_confirmation_accepts_long_number_only_in_case_context():
    assert telegram_bot._airline_confirmation_reference(
        "Your Riyadh Air case was received",
        "Case number: 26073012345678901",
        "RX",
    ) == "26073012345678901"
    assert telegram_bot._airline_confirmation_reference(
        "Your Riyadh Air itinerary",
        "E-ticket number: 26073012345678901",
        "RX",
    ) == ""
    assert telegram_bot._airline_confirmation_reference(
        "Your case was received",
        "Case number: 26073012345678901",
        "SV",
    ) == ""


def test_telegram_transport_errors_never_echo_bot_token():
    class FailedSession:
        def post(self, *_args, **_kwargs):
            raise requests.Timeout(
                "https://api.telegram.org/botvery-secret-token/getUpdates")

    api = TelegramAPI("very-secret-token", session=FailedSession())
    with pytest.raises(RuntimeError) as error:
        api.call("getUpdates")
    assert "very-secret-token" not in str(error.value)
    assert "getUpdates" in str(error.value)


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


def test_notification_outage_does_not_abort_portal_work(
        coordinator, monkeypatch):
    bot, api = coordinator
    monkeypatch.setattr(
        api, "send_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Telegram unavailable")),
    )
    monkeypatch.setattr(
        api, "send_photo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Telegram unavailable")),
    )

    assert bot.notify("Portal work is starting") == {}
    assert bot._send_photo(b"image", "Portal screenshot") == {}


def test_gaca_sms_checker_saves_verified_response_and_explains_bad_reference(
        coordinator, monkeypatch):
    bot, api = coordinator
    airline_id = db.begin_complaint(
        "RX|28|2026-06-16", "airline", "Airline complaint", "Incident")
    db.finish_complaint(airline_id, "submitted", "NOT-A-CASE")
    gaca_id = db.begin_complaint(
        "RX|28|2026-06-16", "gaca", "GACA complaint", "Incident",
        parent_complaint_id=airline_id)
    db.finish_complaint(gaca_id, "submitted", "C076574")
    sms_id, _created = db.save_sms_message({
        "fingerprint": "gaca-status-sms-45",
        "sender": "GACA CARE",
        "received_at": "2026-07-30T09:16:00+03:00",
        "body": "Closed C076574. Details: https://pxpticket.gaca.gov.sa/",
    })
    db.queue_gaca_status_check(
        "C076574", "https://pxpticket.gaca.gov.sa/",
        trigger_sms_id=sms_id, urgent=True)
    monkeypatch.setattr(
        telegram_bot,
        "check_gaca_case",
        lambda *_args, **_kwargs: GacaCaseResult(
            "rejected",
            "rejected",
            (
                "Case status: rejected\n"
                "Provided solution: The supplied airline reference is not a "
                "complaint number."
            ),
            "GACA rejected the case.",
            {},
        ),
    )

    bot.process_gaca_status_checks()

    stored = db.get_complaint(gaca_id)
    assert stored["status"] == "rejected"
    assert "not a complaint number" in stored["provider_response_text"]
    assert db.list_gaca_status_checks()[0]["status"] == "checked"
    messages = "\n".join(item["text"] for item in api.messages)
    assert "GACA response retrieved" in messages
    assert "will not reuse that value" in messages


def test_gaca_checker_automatically_dispatches_verified_prerequisite_once(
        coordinator, monkeypatch):
    bot, _api = coordinator
    airline_id = db.begin_complaint(
        "RX|28|2026-06-16", "airline", "Airline complaint", "Incident")
    db.finish_complaint(airline_id, "submitted", "NOT-A-CASE")
    gaca_id = db.begin_complaint(
        "RX|28|2026-06-16", "gaca", "GACA complaint", "Incident",
        parent_complaint_id=airline_id)
    db.finish_complaint(gaca_id, "submitted", "C076574")
    sms_id, _created = db.save_sms_message({
        "fingerprint": "gaca-auto-remediation",
        "sender": "GACA CARE",
        "received_at": "2026-07-30T09:16:00+03:00",
        "body": "Closed C076574. Details: https://pxpticket.gaca.gov.sa/",
    })
    db.queue_gaca_status_check(
        "C076574", "https://pxpticket.gaca.gov.sa/",
        trigger_sms_id=sms_id, urgent=True)
    monkeypatch.setattr(
        telegram_bot,
        "check_gaca_case",
        lambda *_args, **_kwargs: GacaCaseResult(
            "canceled",
            "canceled",
            (
                "Case status: canceled\nProvided solution: You must first "
                "file a complaint with the airline and wait seven days."
            ),
            "GACA canceled the case.",
            {},
        ),
    )
    dispatched = []
    monkeypatch.setattr(
        bot,
        "refile_airline_after_gaca_prerequisite",
        lambda complaint_id: dispatched.append(complaint_id) or 999,
    )

    bot.process_gaca_status_checks()
    bot._handle_gaca_remediation(
        gaca_id,
        telegram_bot.interpret_gaca_remediation(
            "You must first file a complaint with the airline."),
    )

    assert dispatched == [gaca_id]


def test_startup_backfills_saved_verified_gaca_instruction_once(
        coordinator, monkeypatch):
    bot, _api = coordinator
    airline_id = db.begin_complaint(
        "RX|28|2026-06-16", "airline", "Airline complaint", "Incident")
    db.finish_complaint(airline_id, "submitted", "NOT-A-CASE")
    gaca_id = db.begin_complaint(
        "RX|28|2026-06-16", "gaca", "GACA complaint", "Incident",
        parent_complaint_id=airline_id)
    db.finish_complaint(gaca_id, "submitted", "C076574")
    db.queue_gaca_status_check(
        "C076574", "https://pxpticket.gaca.gov.sa/", urgent=True)
    check = db.claim_due_gaca_status_check()
    db.finish_gaca_status_check(
        int(check["id"]),
        case_status="canceled",
        response_text=(
            "You must first file a complaint with the airline and wait "
            "seven days."),
        response_summary="Airline complaint required.",
    )
    dispatched = []
    monkeypatch.setattr(
        bot,
        "refile_airline_after_gaca_prerequisite",
        lambda complaint_id: dispatched.append(complaint_id) or 999,
    )

    bot._backfill_gaca_remediations()
    bot._backfill_gaca_remediations()

    assert dispatched == [gaca_id]


def test_automatic_gaca_account_sync_pauses_when_nafath_is_required(
        coordinator, monkeypatch):
    bot, _api = coordinator
    db.save_gaca_account_sync(
        "auth_required",
        "The saved session expired. Send /gaca.")
    started = []
    monkeypatch.setattr(
        bot,
        "start_gaca_account_sync",
        lambda **kwargs: started.append(kwargs) or True)

    bot._maybe_sync_gaca_account()

    assert started == []


def test_gaca_account_sync_uses_exponential_error_backoff(
        coordinator, monkeypatch):
    bot, _api = coordinator
    bot.config["gaca_account"].update({
        "sync_minutes": 5,
        "failure_backoff_max_minutes": 60,
    })
    monkeypatch.setattr(
        telegram_bot,
        "sync_gaca_account",
        lambda *_args, **_kwargs: GacaAccountSyncResult(
            "error", "GACA timed out safely."),
    )

    before = time.monotonic()
    bot._run_gaca_account_sync(manual=False)
    first_delay = bot._next_gaca_sync - before
    before = time.monotonic()
    bot._run_gaca_account_sync(manual=False)
    second_delay = bot._next_gaca_sync - before

    assert 300 <= first_delay < 305
    assert 600 <= second_delay < 605


def test_gaca_account_auth_result_stops_background_probes(
        coordinator, monkeypatch):
    bot, _api = coordinator
    monkeypatch.setattr(
        telegram_bot,
        "sync_gaca_account",
        lambda *_args, **_kwargs: GacaAccountSyncResult(
            "auth_required", "Send /gaca."),
    )

    bot._run_gaca_account_sync(manual=False)

    assert bot._next_gaca_sync == float("inf")
    assert bot._gaca_sync_failures == 0


def test_gaca_airline_prerequisite_creates_one_fresh_airline_filing(
        coordinator, monkeypatch):
    bot, api = coordinator
    flight_key = "RXORDER|RX28|2026-06-16"
    db.replace_flights([{
        "flight_key": flight_key,
        "airline_code": "RX",
        "airline_name": "Riyadh Air",
        "pnr": "RXORDER",
        "flight_number": "RX28",
        "flight_numbers": ["RX28"],
        "flight_date": "2026-06-16",
        "origin": "JED",
        "destination": "RUH",
        "passenger": "Test Passenger",
        "email_ids": [],
    }])
    airline_id = db.begin_complaint(
        flight_key,
        "airline",
        "Airline complaint",
        "Original onboard service and privacy incident.",
        issue_summary="Privacy and service failures.",
        requested_resolution_summary="Fair financial compensation.",
    )
    db.finish_complaint(airline_id, "submitted", "ORDER-NOT-CASE")
    gaca_id = db.begin_complaint(
        flight_key,
        "gaca",
        "GACA escalation",
        "Escalation",
        parent_complaint_id=airline_id,
    )
    db.finish_complaint(gaca_id, "submitted", "C076574")
    db.set_complaint_response(
        gaca_id,
        response_text=(
            "Case status: canceled\nProvided solution: "
            "يجب أولاً تقديم الشكوى لدى الناقل الجوي، ثم الانتظار 7 أيام."
        ),
        response_summary="File with the airline first.",
    )
    launched = []

    def start(payload, on_complete, on_update):
        launched.append(payload)
        on_complete(PortalResult(
            "submitted",
            "Riyadh Air accepted the case.",
            "26073012345678901",
        ))

    monkeypatch.setattr(telegram_bot, "start_portal_job", start)
    replacement_id = bot.refile_airline_after_gaca_prerequisite(
        gaca_id,
        incident_override=(
            "انتهاك خصوصيتي بدورة المياه؛ الطعام بارد؛ "
            "باب المقعد مكسور. أطلب تعويضاً."
        ),
    )

    assert replacement_id
    assert db.get_complaint(airline_id)["status"] == "reference_rejected"
    assert db.get_complaint(gaca_id)["status"] == "canceled"
    replacement = db.get_complaint(replacement_id)
    assert replacement["status"] == "submitted"
    assert replacement["reference"] == "26073012345678901"
    assert replacement["parent_complaint_id"] == airline_id
    assert launched[0]["portal_complaint_id"] == replacement_id
    assert launched[0]["airline_code"] == "RX"

    same_id = bot.refile_airline_after_gaca_prerequisite(gaca_id)
    assert same_id == replacement_id
    assert len(launched) == 1
    messages = "\n".join(item["text"] for item in api.messages)
    assert "fresh seven-day GACA timer" in messages
    assert "did not create a duplicate" in messages


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


def test_otp_email_lookup_is_scoped_to_active_alias(coordinator, monkeypatch):
    bot, _api = coordinator
    lookup = {}

    def fetch(_config, *, since, recipient):
        lookup.update(since=since, recipient=recipient)
        return {
            "message_id": "gaca-alias-otp",
            "subject": "GACA EServices - OTP Code",
            "body": "Verification Code: 4821",
        }

    monkeypatch.setattr(
        telegram_bot, "fetch_recent_verification_message", fetch)

    result = bot.request_verification({
        "kind": "otp",
        "message": "Enter OTP",
        "recipient_email": "icrackgames101+2760788@gmail.com",
    })

    assert result == "4821"
    assert lookup["recipient"] == "icrackgames101+2760788@gmail.com"


def test_verification_code_ranking_ignores_footer_year():
    assert telegram_bot._verification_code_from_text(
        "Portal Verification Code: 1078. All rights reserved © 2026"
    ) == "1078"


def test_sms_shortcut_can_complete_active_otp_waiter(coordinator):
    bot, _api = coordinator
    waiter = telegram_bot.VerificationWaiter("otp")
    bot._verification = waiter

    assert bot.accept_verification_code(
        "1 0 7 8", source="test shortcut") is True
    assert waiter.response == "1078"
    assert waiter.event.is_set()


def test_start_registers_telegram_command_menu(coordinator, monkeypatch):
    bot, api = coordinator
    started = []
    monkeypatch.setattr(
        threading.Thread, "start", lambda thread: started.append(thread.name))
    bot.start()
    assert set(started) == {
        "telegram-commands", "telegram-updates", "portal-job-resume",
        "telegram-monitor"}
    bot._register_commands()
    assert api.commands_registered is True


def test_mailbox_scan_runs_off_the_monitor_thread(coordinator, monkeypatch):
    bot, _api = coordinator
    bot.settings["mailbox_scan_minutes"] = 1
    bot.config["imap"].update({
        "user": "passenger@example.com", "password": "app-password",
    })
    started = threading.Event()
    release = threading.Event()

    def fake_scan(*_args, **_kwargs):
        started.set()
        release.wait(2)

    monkeypatch.setattr(telegram_bot, "scan_mailbox", fake_scan)

    bot._maybe_scan_mailbox()

    assert started.wait(1)
    assert bot._mail_scan_thread is not None
    assert bot._mail_scan_thread.is_alive()
    release.set()
    bot._mail_scan_thread.join(2)
    assert bot._mail_scan_error == ""


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


def test_reopen_allows_new_complaint_after_closed_resolved(coordinator):
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    first = db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken screen")
    db.finish_complaint(first, "submitted", "C_9000001")
    assert db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Still broken") is None
    reopen = db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Still broken",
        reopen=True)
    assert reopen is not None
    assert db.complaints_for_flight(flight["flight_key"])[0]["status"] == "closed"


def test_finish_complaint_updates_stale_pending_portal_jobs(coordinator):
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    complaint_id = db.begin_complaint(
        flight["flight_key"], "airline", "Claim", "Broken screen")
    db.finish_complaint(complaint_id, "accepted_pending_reference")
    db.save_portal_job({
        "id": "pending-ref-job", "kind": "airline", "airline_code": "SV",
        "flight_number": flight.get("flight_number") or "SV100",
        "flight_key": flight["flight_key"],
        "status": "accepted_pending_reference",
        "message": "Waiting for reference", "reference": "", "terminal": True,
    })
    db.finish_complaint(complaint_id, "submitted", "C_9050001")
    job = db.list_portal_jobs(5)[0]
    assert job["status"] == "success"
    assert job["reference"] == "C_9050001"


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
    while (len(api.edits) < 3 or len(api.photos) < 2) and time.time() < deadline:
        time.sleep(.01)

    assert "Doing now: opening the official website" in api.messages[0]["text"]
    assert api.photos[0]["image"] == b"loaded-screen"
    assert "Finished: opening the official website" in api.photos[0]["caption"]
    assert "Doing now: filling the complaint form" in api.photos[0]["caption"]
    assert len(api.messages) == 1
    assert len(api.photos) == 2
    assert "Result: complaint submitted" in api.edits[-1]["text"]


def test_reference_reconciliation_sends_one_final_screenshot(coordinator):
    bot, api = coordinator
    db.replace_flights([{
        "flight_key": "RXBOOKING|RX28|2026-06-16",
        "airline_code": "RX",
        "airline_name": "Riyadh Air",
        "flight_number": "RX28",
        "flight_date": "2026-06-16",
        "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        "RXBOOKING|RX28|2026-06-16",
        "gaca",
        "On-board service complaint",
    )
    screenshot = telegram_bot.TELEGRAM_EVIDENCE_DIR / "gaca-success.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"success-screen")
    db.save_portal_job({
        "id": "gaca-final",
        "kind": "gaca",
        "flight_key": "RXBOOKING|RX28|2026-06-16",
        "flight_number": "RX28",
        "complaint_id": complaint_id,
        "status": "submitted",
        "reference": "C076574",
        "terminal": True,
        "screenshot_file": str(screenshot),
    })

    bot.notify_reference_reconciled(
        complaint_id, "C076574", source="SMS")

    assert len(api.photos) == 1
    assert api.photos[0]["image"] == b"success-screen"
    assert "RX28" in api.photos[0]["caption"]
    assert "Riyadh Air" in api.photos[0]["caption"]
    assert "C076574" in api.photos[0]["caption"]
    assert "no duplicate" in api.photos[0]["caption"]
    assert api.messages == []


def test_due_flight_gets_one_telegram_survey(coordinator):
    bot, api = coordinator
    assert load_demo(log=lambda *_args, **_kwargs: None) == 3
    flight = db.list_flights()[0]
    arrival = (datetime.now() - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")
    db.set_overrides(flight["id"], {
        "arrival": arrival, "cancelled": False})
    bot.send_due_surveys(now=datetime.now())
    assert len(api.messages) == 1
    assert "How was" in api.messages[0]["text"]
    assert " from " in api.messages[0]["text"]
    assert " to " in api.messages[0]["text"]
    assert db.survey_for_flight(flight["flight_key"])["status"] == "asked"
    bot.send_due_surveys(now=datetime.now())
    assert len(api.messages) == 1


def test_flight_checkins_use_configured_local_timezone(coordinator):
    bot, _api = coordinator
    local_now = bot._flight_local_now()
    assert local_now.tzinfo is None
    assert abs((local_now - datetime.now(UTC).replace(tzinfo=None)
                - timedelta(hours=3))
               .total_seconds()) < 5


def test_cancelled_flight_gets_cancellation_specific_checkin(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    arrival = (datetime.now() - timedelta(minutes=15)).strftime(
        "%Y-%m-%d %H:%M")
    db.set_overrides(flight["id"], {
        "arrival": arrival, "cancelled": True})

    bot.send_due_surveys(now=datetime.now())

    assert len(api.messages) == 1
    assert "was cancelled" in api.messages[0]["text"]
    buttons = api.messages[0]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["callback_data"].startswith("flight_no_issue:")
    assert buttons[1][0]["callback_data"].startswith("flight_issue:")
    assert db.survey_for_flight(flight["flight_key"])["status"] == "asked"
    bot.send_due_surveys(now=datetime.now())
    assert len(api.messages) == 1


def test_retracted_cancellation_prompt_cannot_open_complaint_intake(
        coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], bot.chat_id, 800, "asked")
    db.update_survey_status(flight["flight_key"], "retracted")

    bot._handle_callback({
        "id": "old-cancellation-button",
        "data": f"flight_issue:{flight['id']}",
        "message": {"chat": {"id": 42}},
    })

    assert "check-in was retracted" in api.messages[-1]["text"]
    assert db.survey_for_flight(flight["flight_key"])["status"] == "retracted"


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


def test_gaca_command_starts_read_only_account_sync(coordinator, monkeypatch):
    bot, api = coordinator
    captured = {}

    def fake_sync(config, update, *, allow_login=False):
        captured["allow_login"] = allow_login
        update("opening", "Opening GACA account", None)
        return GacaAccountSyncResult(
            "success", "Imported 2 GACA cases.", cases_seen=2,
            cases_mapped=2)

    monkeypatch.setattr(telegram_bot, "sync_gaca_account", fake_sync)
    bot.handle_update({"message": {
        "message_id": 80, "chat": {"id": 42}, "text": "/gaca"}})
    bot._gaca_sync_thread.join(2)

    assert captured["allow_login"] is True
    assert any("read-only GACA account sync" in item["text"]
               for item in api.messages)
    assert any("Imported 2 GACA cases" in item["text"]
               for item in api.messages)


def test_callback_press_is_journaled_with_safe_action(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]

    bot.handle_update({"callback_query": {
        "id": "callback-1",
        "data": f"flight_good:{flight['id']}",
        "message": {"message_id": 700, "chat": {"id": 42}},
    }})

    journal = db.list_telegram_messages(10)
    callback = next(item for item in journal
                    if item.get("media_kind") == "callback")
    assert callback["direction"] == "incoming"
    assert callback["text"] == "[Button pressed: flight good]"
    assert callback["reply_to_message_id"] == 700
    assert api.callbacks == [("callback-1", "")]


def test_held_gaca_duplicate_prompts_reference_recovery_once(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    airline_id = db.add_complaint(
        flight["flight_key"], "airline", None, "Seat complaint",
        "closed", reference="C_2760788", details="Broken screen.")
    gaca_id = db.add_complaint(
        flight["flight_key"], "gaca", None, "Escalation", "filing",
        details="Broken screen.", parent_complaint_id=airline_id)
    db.save_portal_job({
        "id": "held-duplicate",
        "kind": "gaca",
        "flight_key": flight["flight_key"],
        "complaint_id": gaca_id,
        "status": "held",
        "message": (
            "GACA reports this escalation as a duplicate; held for recovery."),
        "terminal": False,
        "payload": {"kind": "gaca"},
    })

    threads = [
        threading.Thread(target=bot._prompt_held_gaca_duplicates)
        for _index in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    recovery_messages = [
        item for item in api.messages
        if "Recover existing GACA case" in str(item.get("reply_markup"))
    ]
    assert len(recovery_messages) == 1
    button = recovery_messages[0]["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == f"gaca_recover:{flight['id']}"

    launched = []
    monkeypatch.setattr(
        bot, "start_gaca_account_sync",
        lambda manual=False: launched.append(manual))
    bot.handle_update({"callback_query": {
        "id": "callback-recover",
        "data": button["callback_data"],
        "message": {
            "message_id": recovery_messages[0]["message_id"],
            "chat": {"id": 42},
        },
    }})

    assert launched == [True]
    assert any(
        item["text"] == "[Button pressed: gaca recover]"
        for item in db.list_telegram_messages(20))


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
    assert len(db.complaints_for_flight(flight["flight_key"])) == 1


def test_reference_less_resolutions_are_not_matched_by_fifo(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Older claim", "submitted",
        reference="C_7000001", details="The first screen was broken.")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Newer claim", "submitted",
        reference="C_7000002", details="The second screen was broken.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-2 days') "
            "WHERE reference = 'C_7000001'")
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-1 day') "
            "WHERE reference = 'C_7000002'")
    db.save_mail_event({
        "message_id": "<fifo-resolution-1@example>",
        "subject": "Your complaint resolution",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "Our review is complete. Compensation was declined and the case is closed.",
    })
    db.save_mail_event({
        "message_id": "<fifo-resolution-2@example>",
        "subject": "Your complaint resolution",
        "sender": "customer.relations@saudia.com",
        "date": datetime.now() + timedelta(seconds=1),
        "body": "Our review is complete. A refund was approved and the case is resolved.",
    })

    bot.check_complaint_responses()

    assert db.list_complaint_responses() == []
    assert api.messages == []


def test_reference_less_resolution_uses_strong_booking_facts(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    older_flight = next(item for item in db.list_flights()
                        if item.get("airline_code") == "SV")
    newer_data = dict(older_flight)
    newer_data.update({
        "flight_key": "NEW999|SV1999|2026-07-09",
        "pnr": "NEW999", "flight_number": "SV1999",
        "flight_numbers": ["SV1999"], "flight_date": "2026-07-09",
        "passenger": "Fatimah Example", "email_ids": [],
    })
    newer_data.pop("id", None)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO flights (flight_key, data, overrides) VALUES (?, ?, '{}')",
            (newer_data["flight_key"], json.dumps(newer_data)))
    db.add_complaint(
        older_flight["flight_key"], "airline", None, "Older claim", "submitted",
        reference="C_7050001", details="Older issue.")
    db.add_complaint(
        newer_data["flight_key"], "airline", None, "Newer claim", "submitted",
        reference="C_7050002", details="Newer issue.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-2 days') "
            "WHERE reference = 'C_7050001'")
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-1 day') "
            "WHERE reference = 'C_7050002'")
    event_id = db.save_mail_event({
        "message_id": "<fact-resolution@example>",
        "subject": "Complaint resolution for flight SV1999",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": ("For Fatimah Example, booking NEW999 on 2026-07-09: "
                 "our review is complete and compensation was declined."),
    })

    bot.check_complaint_responses()

    links = db.list_complaint_responses()
    newer = next(item for item in db.list_complaints()
                 if item.get("reference") == "C_7050002")
    assert [(item["complaint_id"], item["mail_event_id"], item["match_method"])
            for item in links] == [(newer["id"], event_id, "case_facts")]
    assert len(api.messages) == 1
    assert "booking facts" in api.messages[0]["text"].casefold()


def test_reference_match_wins_even_when_resolutions_arrive_out_of_order(
        coordinator):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Older claim", "submitted",
        reference="C_7100001", details="First issue.")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Newer claim", "submitted",
        reference="C_7100002", details="Second issue.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-2 days') "
            "WHERE reference = 'C_7100001'")
        conn.execute(
            "UPDATE complaints SET created_at = datetime('now', '-1 day') "
            "WHERE reference = 'C_7100002'")
    newer_event = db.save_mail_event({
        "message_id": "<newer-first@example>",
        "subject": "Ticket C_7100002 resolved",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "C_7100002 was reviewed and closed.",
    })
    older_event = db.save_mail_event({
        "message_id": "<older-second@example>",
        "subject": "Ticket C_7100001 resolved",
        "sender": "customer.relations@saudia.com",
        "date": datetime.now() + timedelta(seconds=1),
        "body": "C_7100001 was reviewed and closed.",
    })

    bot.check_complaint_responses()
    links = db.list_complaint_responses()
    complaints = {item["reference"]: item for item in db.list_complaints()}
    assert {(item["complaint_id"], item["mail_event_id"], item["match_method"])
            for item in links} == {
        (complaints["C_7100002"]["id"], newer_event, "exact_reference"),
        (complaints["C_7100001"]["id"], older_event, "exact_reference"),
    }


def test_acknowledgement_does_not_link_without_reference_or_strong_facts(
        coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="C_7200001", details="Broken screen.")
    db.save_mail_event({
        "message_id": "<generic-ack@example>", "subject": "We received your message",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "Thank you for contacting us. We will review your message.",
    })
    db.save_mail_event({
        "message_id": "<generic-resolution@example>",
        "subject": "Complaint resolution",
        "sender": "customer.relations@saudia.com",
        "date": datetime.now() + timedelta(seconds=1),
        "body": "The review is complete and compensation was declined. The case is closed.",
    })

    bot.check_complaint_responses()

    assert db.list_complaint_responses() == []
    assert api.messages == []


def test_closure_notice_that_points_to_email_waits_for_details(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="C_2781202", details="Broken screen.")
    db.save_mail_event({
        "message_id": "<closure@example>",
        "subject": "Your Ticket C_2781202 is Closed",
        "sender": "CR-NORPLY@saudia.com", "date": datetime.now(),
        "body": (
            "Service Ticket -Closure Notification. We have finalized your "
            "comment No. C_2781202 and email has been sent to your mail address."),
    })

    bot.check_complaint_responses()
    bot.check_complaint_responses()

    assert len(api.messages) == 1
    assert "contains no outcome or remedy" in api.messages[0]["text"]
    assert "checking Gmail for the detailed message" in api.messages[0]["text"]
    assert "will not reopen or escalate merely because of this notice" in (
        api.messages[0]["text"])
    assert api.messages[0]["reply_markup"] is None
    assert db.list_complaint_responses() == []
    assert len(db.complaints_for_flight(flight["flight_key"])) == 1


def test_creation_notice_email_footer_does_not_become_closure(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="C_2817700", details="Broken screen.")
    db.save_mail_event({
        "message_id": "<creation-notice@example>",
        "subject": "Your Ticket C_2817700 is Registered with us",
        "sender": "CR-NORPLY@saudia.com",
        "date": datetime.now(),
        "body": (
            "Service Ticket - Creation Notification. This is an auto response "
            "confirming we received your comment. A specialist will review and "
            "reply within 15 days. The recipient should check this email and "
            "attachments for viruses."),
    })

    bot.check_complaint_responses()
    bot.check_complaint_responses()

    assert api.messages == []
    assert db.list_complaint_responses() == []
    complaint = db.complaints_for_flight(flight["flight_key"])[0]
    assert not db.event_seen(f"airline-responded:{complaint['id']}")
    assert not db.event_seen(f"closure-details-pending:{complaint['id']}")


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


def test_existing_reference_is_not_copied_to_pending_case_after_restart(
        coordinator):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Original complaint",
        "submitted", reference="C_2778782", details="Amenity kit issue.")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Later complaint",
        "accepted_pending_reference", details="A later unrelated issue.")
    db.save_mail_event({
        "message_id": "<duplicate-confirmation@example>",
        "subject": "Your Ticket C_2778782 is Registered with us",
        "sender": "CR-NORPLY@saudia.com", "date": datetime.now(),
        "body": "Thank you for contacting Saudia Guest Relations.",
    })

    bot.check_complaint_responses()

    complaints = db.complaints_for_flight(flight["flight_key"])
    owners = [item for item in complaints
              if item.get("reference") == "C_2778782"]
    pending = next(item for item in complaints
                   if item.get("subject") == "Later complaint")
    assert len(owners) == 1
    assert pending["status"] == "accepted_pending_reference"
    assert pending["reference"] is None


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
    assert len(db.complaints_for_flight(flight["flight_key"])) == 1


def test_non_substantive_ai_response_analysis_is_cached(coordinator):
    bot, api = coordinator

    class FakeGhala:
        enabled = True
        name = "Ghala"
        model = "claude-test"
        settings = {"analyze_responses": True}

        def __init__(self):
            self.calls = 0

        def analyze_response(self, *_args):
            self.calls += 1
            return {
                "summary": "The airline only acknowledged the case.",
                "outcome": "pending", "amounts_or_deadlines": [],
                "recommendation": "wait", "rationale": "No decision yet.",
                "substantive": False,
            }

    bot.ai = FakeGhala()
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="C_7300001", details="Broken screen")
    db.save_mail_event({
        "message_id": "<cached-ai-ack@example>",
        "subject": "Case C_7300001 update",
        "sender": "customer.relations@saudia.com", "date": datetime.now(),
        "body": "We are reviewing C_7300001 and will contact you later.",
    })

    bot.check_complaint_responses()
    bot.check_complaint_responses()

    assert bot.ai.calls == 1
    assert api.messages == []


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
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-333444", details="The seat was broken.",
        attachments=["seat.jpg"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE reference = ?",
            ((datetime.now() - timedelta(days=8)).strftime(
                "%Y-%m-%d %H:%M:%S"), "CAS-333444"))
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


def test_gaca_acceptance_without_reference_stays_pending_not_failed(
        coordinator, monkeypatch):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Claim", "submitted",
        reference="CAS-333445", details="The seat was broken.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE reference = ?",
            ((datetime.now() - timedelta(days=8)).strftime(
                "%Y-%m-%d %H:%M:%S"), "CAS-333445"))
    monkeypatch.setattr(
        telegram_bot, "missing_portal_fields", lambda _payload: [])

    def fake_start(_payload, on_complete, on_update=None):
        on_complete(PortalResult(
            "accepted_pending_reference", "accepted without reference"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_start)
    bot._handle_callback({
        "id": "callback-gaca-pending",
        "data": f"escalate:{flight['id']}",
        "message": {"chat": {"id": 42}},
    })
    complaint = db.complaints_for_flight(flight["flight_key"])[-1]
    assert complaint["kind"] == "gaca"
    assert complaint["status"] == "accepted_pending_reference"
    assert complaint["reference"] is None


def _wait_for_ai(bot, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = bot._ai_chat_thread
        if thread and not thread.is_alive():
            return
        time.sleep(.01)
    pytest.fail("Telegram AI worker did not finish")


def test_plain_telegram_request_uses_ai_intent_then_exact_flight_lookup(
        coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    number = flight.get("flight_number") or flight.get("flight_numbers")[0]

    class FakeGhala:
        enabled = True
        name = "Ghala"
        last_error = ""

        def interpret_telegram(self, message, catalog):
            assert number in message
            assert any(item["flight_number"] == number
                       for item in catalog["flights"])
            return {"actions": [{
                "name": "flight_details", "flight_number": number,
                "pnr": "", "reference": "", "passenger": "", "query": "",
                "time_scope": "all", "latest": False, "limit": 5,
            }], "reply": ""}

    bot.ai = FakeGhala()
    bot.handle_update({"message": {
        "message_id": 991, "chat": {"id": 42},
        "text": f"Can you show me everything for {number}?",
    }})
    _wait_for_ai(bot)

    assert "checking your FlightDeck records" in api.messages[0]["text"]
    assert api.messages[1]["text"].startswith("Flight:")
    assert number in api.messages[1]["text"]
    assert "Payment:" in api.messages[1]["text"]


def test_status_command_stays_deterministic_and_does_not_call_ai(coordinator):
    bot, api = coordinator

    class FakeGhala:
        enabled = True
        name = "Ghala"
        model = "claude-test"
        last_error = ""
        settings = {"extract_profile_evidence": True}

        def interpret_telegram(self, *_args):
            raise AssertionError("slash command must not reach AI")

    bot.ai = FakeGhala()
    bot.handle_update({"message": {
        "message_id": 992, "chat": {"id": 42}, "text": "/status",
    }})

    assert len(api.messages) == 1
    assert api.messages[0]["text"].startswith("FlightDeck is running.")
    assert bot._ai_chat_thread is None


def test_plain_telegram_request_can_return_latest_portal_screenshot(
        coordinator):
    bot, api = coordinator
    folder = telegram_bot.TELEGRAM_EVIDENCE_DIR / "portal_jobs"
    folder.mkdir(parents=True)
    (folder / "job.png").write_bytes(b"saved-portal-image")

    class FakeGhala:
        enabled = True
        name = "Ghala"
        last_error = ""

        def interpret_telegram(self, *_args):
            return {"actions": [{
                "name": "latest_screenshot", "flight_number": "", "pnr": "",
                "reference": "", "passenger": "", "query": "",
                "time_scope": "all", "latest": True, "limit": 1,
            }], "reply": ""}

    bot.ai = FakeGhala()
    bot.handle_update({"message": {
        "message_id": 993, "chat": {"id": 42},
        "text": "Send me the latest portal screenshot",
    }})
    _wait_for_ai(bot)

    assert len(api.photos) == 1
    assert api.photos[0]["image"] == b"saved-portal-image"
    assert "Saved portal screenshot" in api.photos[0]["caption"]


def test_pending_post_flight_survey_keeps_priority_over_ai(coordinator,
                                                            monkeypatch):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], bot.chat_id, 700,
                     "awaiting_details")
    collected = []
    monkeypatch.setattr(bot, "_collect_issue",
                        lambda survey, message: collected.append((survey, message)))

    class FakeGhala:
        enabled = True
        name = "Ghala"

        def interpret_telegram(self, *_args):
            raise AssertionError("survey reply must not reach AI")

    bot.ai = FakeGhala()
    message = {
        "message_id": 994, "chat": {"id": 42},
        "reply_to_message": {"message_id": 700},
        "text": "The screen was broken.",
    }
    bot.handle_update({"message": message})

    assert len(collected) == 1
    assert collected[0][1] == message
    assert bot._ai_chat_thread is None


def test_portal_question_bypasses_unrelated_pending_intake_and_gets_job_context(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], bot.chat_id, 703,
                     "awaiting_details")
    screenshot = telegram_bot.TELEGRAM_EVIDENCE_DIR / "portal_jobs" / "job.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"portal-error")
    db.save_portal_job({
        "id": "job-1", "kind": "airline", "airline_code": "SV",
        "flight_number": "SV1650", "status": "error",
        "message": "Submission rejected", "reference": "",
        "screenshot_file": str(screenshot), "terminal": True,
    })
    collected = []
    monkeypatch.setattr(bot, "_collect_issue",
                        lambda *_args: collected.append(True))

    class FakeGhala:
        enabled = True
        name = "Ghala"
        last_error = ""

        def interpret_telegram(self, message, catalog):
            assert message == "Why did the portal fail? I can see the captcha."
            assert catalog["recent_portal_jobs"][0]["status"] == "error"
            assert any(
                item["direction"] == "incoming"
                and "captcha" in item["text"]
                for item in catalog["recent_conversation"])
            return {"actions": [{
                "name": "portal_status", "flight_number": "", "pnr": "",
                "reference": "", "passenger": "", "query": message,
                "time_scope": "all", "latest": True, "limit": 1,
            }], "reply": ""}

    bot.ai = FakeGhala()
    bot.handle_update({"message": {
        "message_id": 997, "chat": {"id": 42},
        "text": "Why did the portal fail? I can see the captcha.",
    }})
    _wait_for_ai(bot)

    assert collected == []
    assert any("Stage: error" in message["text"] for message in api.messages)
    assert api.photos[-1]["image"] == b"portal-error"


def test_unanswered_checkin_does_not_swallow_unrelated_ai_question(coordinator):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], bot.chat_id, 701, "asked")

    class FakeGhala:
        enabled = True
        name = "Ghala"
        last_error = ""

        def interpret_telegram(self, message, _catalog):
            assert message == "show my latest complaint"
            return {"actions": [{
                "name": "help", "flight_number": "", "pnr": "",
                "reference": "", "passenger": "", "query": "",
                "time_scope": "all", "latest": False, "limit": 5,
            }], "reply": ""}

    bot.ai = FakeGhala()
    bot.handle_update({"message": {
        "message_id": 995, "chat": {"id": 42},
        "text": "show my latest complaint",
    }})
    _wait_for_ai(bot)

    assert len(api.messages) == 2
    assert "checking your FlightDeck records" in api.messages[0]["text"]
    assert "Ask naturally" in api.messages[1]["text"]
    assert db.survey_for_flight(flight["flight_key"])["status"] == "asked"


def test_clear_issue_text_still_answers_unreplied_post_flight_checkin(
        coordinator, monkeypatch):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = db.list_flights()[0]
    db.record_survey(flight["flight_key"], bot.chat_id, 702, "asked")
    collected = []
    monkeypatch.setattr(bot, "_collect_issue",
                        lambda survey, message: collected.append((survey, message)))

    bot.handle_update({"message": {
        "message_id": 996, "chat": {"id": 42},
        "text": "The baggage was damaged and the screen was broken.",
    }})

    assert len(collected) == 1
    assert collected[0][0]["flight_key"] == flight["flight_key"]
    assert bot._ai_chat_thread is None


def test_short_ai_follow_up_reuses_last_exact_complaint_not_another_case(
        coordinator, tmp_path):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flights = db.list_flights()
    first_photo = tmp_path / "first.jpg"
    second_photo = tmp_path / "second.jpg"
    first_photo.write_bytes(b"first-case-photo")
    second_photo.write_bytes(b"second-case-photo")
    db.add_complaint(
        flights[0]["flight_key"], "airline", None, "First", "submitted",
        reference="C_8000001", details="First issue",
        attachments=[str(first_photo)])
    db.add_complaint(
        flights[1]["flight_key"], "airline", None, "Second", "submitted",
        reference="C_8000002", details="Second issue",
        attachments=[str(second_photo)])
    base = {
        "flight_number": "", "pnr": "", "passenger": "", "query": "",
        "time_scope": "all", "latest": False, "limit": 5,
    }

    bot._send_complaint_details({
        **base, "name": "complaint_details", "reference": "C_8000001",
    })
    follow_up = bot._contextualize_ai_action({
        **base, "name": "show_evidence", "reference": "",
    }, "send its photos")
    bot._execute_ai_action(follow_up)

    assert follow_up["reference"] == "C_8000001"
    assert len(api.photos) == 1
    assert api.photos[0]["image"] == b"first-case-photo"


def test_explicit_details_request_corrects_an_ai_list_intent(coordinator):
    bot, _api = coordinator
    base = {
        "name": "list_complaints", "flight_number": "", "pnr": "",
        "reference": "", "passenger": "", "query": "",
        "time_scope": "all", "latest": True, "limit": 1,
    }

    actual = bot._contextualize_ai_action(
        base, "show me my latest complaint details")

    assert actual["name"] == "complaint_details"


def test_seven_day_no_response_auto_escalates_once(coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
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


def test_auto_escalation_silently_recognizes_active_gaca_job(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Seat complaint",
        "submitted", reference="CAS-700020",
        details="The seat door was broken.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at=datetime('now', '-8 days') "
            "WHERE reference='CAS-700020'")
    airline = db.complaints_for_flight(flight["flight_key"])[0]
    db.add_complaint(
        flight["flight_key"], "gaca", None, "Active GACA escalation",
        "filing", details="The seat door was broken.",
        parent_complaint_id=int(airline["id"]))
    monkeypatch.setattr(
        telegram_bot, "missing_portal_fields", lambda _payload: [])

    assert bot._launch_gaca(
        db.get_flight(flight["id"]), automatic=True) is False

    assert not any(
        "existing GACA filing" in item["text"]
        for item in api.messages)


def test_manual_gaca_duplicate_reports_real_active_job_state(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    airline_id = db.add_complaint(
        flight["flight_key"], "airline", None, "Seat complaint",
        "submitted", reference="CAS-700022",
        details="The seat door was broken.")
    gaca_id = db.add_complaint(
        flight["flight_key"], "gaca", None, "Active GACA escalation",
        "filing", details="The seat door was broken.",
        parent_complaint_id=airline_id)
    db.save_portal_job({
        "id": "active-manual-gaca",
        "kind": "gaca",
        "flight_key": flight["flight_key"],
        "complaint_id": gaca_id,
        "status": "verification",
        "message": "Waiting for the required ticket number",
        "terminal": False,
        "payload": {"kind": "gaca"},
    })
    monkeypatch.setattr(
        telegram_bot, "missing_portal_fields", lambda _payload: [])

    assert bot._launch_gaca(
        db.get_flight(flight["id"]), automatic=False,
        prior_complaint=db.get_complaint(airline_id)) is False

    assert api.messages[-1]["text"] == (
        "The existing GACA filing is still verification: "
        "Waiting for the required ticket number. "
        "I did not start a second submission.")
    assert "on record" not in api.messages[-1]["text"]


def test_old_filing_with_active_portal_job_is_not_replaced(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Seat complaint",
        "submitted", reference="CAS-700021",
        details="The seat door was broken.")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at=datetime('now', '-8 days') "
            "WHERE reference='CAS-700021'")
    airline = db.complaints_for_flight(flight["flight_key"])[0]
    gaca_id = db.add_complaint(
        flight["flight_key"], "gaca", None, "Active GACA escalation",
        "filing", details="The seat door was broken.",
        parent_complaint_id=int(airline["id"]),
        created_at=(
            datetime.now() - timedelta(hours=2)
        ).strftime("%Y-%m-%d %H:%M:%S"))
    db.save_portal_job({
        "id": "active-old-gaca",
        "kind": "gaca",
        "flight_key": flight["flight_key"],
        "complaint_id": gaca_id,
        "status": "reviewing",
        "message": "Reviewing the official form",
        "terminal": False,
        "payload": {
            "kind": "gaca",
            "flight_key": flight["flight_key"],
            "portal_complaint_id": gaca_id,
        },
    })
    monkeypatch.setattr(
        telegram_bot, "missing_portal_fields", lambda _payload: [])

    assert db.begin_complaint(
        flight["flight_key"], "gaca", "Duplicate", "Duplicate") is None
    assert db.get_complaint(gaca_id)["status"] == "filing"
    assert bot._launch_gaca(
        db.get_flight(flight["id"]), automatic=True) is False

    gaca_rows = [
        item for item in db.complaints_for_flight(flight["flight_key"])
        if item["kind"] == "gaca"
    ]
    assert [item["id"] for item in gaca_rows] == [gaca_id]
    assert not any(
        "existing GACA filing" in item["text"]
        for item in api.messages)


def test_manual_gaca_submission_cannot_bypass_seven_day_gate(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Recent airline complaint",
        "submitted", reference="CAS-700010", details="Broken screen")
    flight = db.get_flight(flight["id"])

    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("GACA must not open before the seven-day gate")

    monkeypatch.setattr(telegram_bot, "start_portal_job", forbidden_start)

    assert bot._launch_gaca(flight) is False
    assert bot._launch_gaca(flight) is False
    assert sum(
        "handling window has not finished" in item["text"]
        for item in api.messages
    ) == 1
    assert not any(
        item["kind"] == "gaca"
        for item in db.complaints_for_flight(flight["flight_key"])
    )


def test_gaca_submission_skips_incidents_older_than_sixty_days(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(flight["id"], {"flight_date": "2025-01-01"})
    db.add_complaint(
        flight["flight_key"], "airline", None, "Old airline complaint",
        "submitted", reference="CAS-700012", details="Broken screen")
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE reference = ?",
            ("2025-01-02 00:00:00", "CAS-700012"))
    flight = db.get_flight(flight["id"])

    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("GACA must not open after the 60-day incident limit")

    monkeypatch.setattr(telegram_bot, "start_portal_job", forbidden_start)

    assert bot._launch_gaca(flight) is False
    assert bot._launch_gaca(flight) is False
    assert sum(
        "more than 60 days old" in item["text"]
        for item in api.messages
    ) == 1


def test_gaca_confirmation_unknown_is_reconciled_not_resubmitted(
        coordinator, monkeypatch):
    bot, api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Airline complaint",
        "submitted", reference="CAS-700011", details="Delayed baggage")
    airline = db.complaints_for_flight(flight["flight_key"])[0]
    db.add_complaint(
        flight["flight_key"], "gaca", None, "GACA escalation", "failed",
        details="The airline complaint remains unresolved.",
        parent_complaint_id=int(airline["id"]))
    gaca = db.complaints_for_flight(flight["flight_key"])[-1]
    db.save_portal_job({
        "id": "gaca-unknown-test",
        "kind": "gaca",
        "complaint_id": int(gaca["id"]),
        "status": "confirmation_unknown",
        "message": "Submit clicked; confirmation unreadable",
        "terminal": True,
    })
    flight = db.get_flight(flight["id"])

    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("an ambiguous GACA submit must not be repeated")

    monkeypatch.setattr(telegram_bot, "start_portal_job", forbidden_start)

    assert bot._launch_gaca(flight, prior_complaint=airline) is False
    assert bot._launch_gaca(flight, prior_complaint=airline) is False
    assert sum(
        "already sent once" in item["text"] for item in api.messages
    ) == 1


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
    assert not db.event_seen(f"auto-gaca:{complaint_id}")


def test_auto_gaca_marker_is_written_only_after_successful_submission(
        coordinator, monkeypatch):
    bot, _api = coordinator
    load_demo(log=lambda *_args, **_kwargs: None)
    flight = next(item for item in db.list_flights()
                  if item.get("airline_code") == "SV")
    db.set_overrides(
        flight["id"], {"flight_date": datetime.now().date().isoformat()})
    flight = db.get_flight(flight["id"])
    db.add_complaint(
        flight["flight_key"], "airline", None, "Seat screen complaint",
        "submitted", reference="CAS-700099",
        details="The seat-back entertainment screen was broken.")
    submitted_at = datetime.now() - timedelta(days=8)
    with db.connect() as conn:
        conn.execute(
            "UPDATE complaints SET created_at = ? WHERE reference = ?",
            (submitted_at.strftime("%Y-%m-%d %H:%M:%S"), "CAS-700099"))
    airline_complaint = db.complaints_for_flight(flight["flight_key"])[0]
    monkeypatch.setattr(telegram_bot, "missing_portal_fields", lambda _payload: [])

    def fake_fail(payload, on_complete, on_update=None):
        on_complete(PortalResult("needs_attention", "Page.goto timeout"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_fail)
    bot.auto_escalate_due_complaints(now=datetime.now())
    assert not db.event_seen(f"auto-gaca:{airline_complaint['id']}")
    assert not db.event_seen(f"auto-gaca-inflight:{airline_complaint['id']}")

    def fake_ok(payload, on_complete, on_update=None):
        on_complete(PortalResult("submitted", "ok", "GACA-700099"))
        return "job"

    monkeypatch.setattr(telegram_bot, "start_portal_job", fake_ok)
    bot.auto_escalate_due_complaints(now=datetime.now())
    assert db.event_seen(f"auto-gaca:{airline_complaint['id']}")


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


def test_telegram_ticket_preview_confirm_and_manual_reference(
        coordinator):
    bot, api = coordinator
    bot.handle_update({"message": {
        "message_id": 1901,
        "chat": {"id": 42},
        "text": (
            "/ticket Airline: Saudia\n"
            "Passenger: Muhannad Albu Asais\n"
            "PNR: TG7ABC\n"
            "E-ticket number: 065-1234567890\n"
            "Flight number: SV520\n"
            "Flight date: 2026-07-15\n"
            "Route: RUH to BAH\n"
            "Departure: 2026-07-15 10:15\n"
            "Arrival: 2026-07-15 11:30\n"
            "Manual complaint C_2779999 filed 2026-07-20 "
            "about the seat screen was broken."
        ),
    }})
    deadline = time.time() + 3
    draft = None
    while time.time() < deadline:
        draft = db.latest_pending_ticket_import("42")
        if draft and draft["status"] == "ready":
            break
        time.sleep(.01)
    assert draft and draft["status"] == "ready"
    preview = api.messages[-1]
    buttons = preview["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["text"] == "Add to dashboard"
    assert "Muhannad Albu Asais" in preview["text"]
    assert "C_2779999" in preview["text"]

    bot.handle_update({"callback_query": {
        "id": "ticket-confirm-1",
        "data": f"ticket_confirm:{draft['token']}",
        "message": {"chat": {"id": 42}},
    }})

    flights = db.list_flights()
    assert len(flights) == 1
    assert flights[0]["passenger"] == "Muhannad Albu Asais"
    assert flights[0]["flight_number"] == "SV520"
    complaint = db.list_complaints()[0]
    assert complaint["reference"] == "C_2779999"
    assert complaint["submission_source"] == "manual_telegram"
    assert complaint["created_at"] == "2026-07-20 00:00:00"
    assert "screen was broken" in complaint["submitted_text"]
    assert db.get_ticket_import(draft["token"])["status"] == "imported"


def test_manual_reference_refuses_ambiguous_family_flights(
        coordinator):
    bot, api = coordinator
    raw = [
        {
            "message_id": "<family-one>",
            "subject": "E-ticket SV1671",
            "sender": "tickets@saudia.com",
            "date": datetime(2026, 7, 1),
            "body": (
                "Passenger: Mansour Albu Asais\nPNR: MAN123\n"
                "Ticket number: 065-1111111111\nFlight SV1671\n"
                "Flight date: 15 July 2026\nRUH to AHB"),
        },
        {
            "message_id": "<family-two>",
            "subject": "E-ticket SV1671",
            "sender": "tickets@saudia.com",
            "date": datetime(2026, 7, 1),
            "body": (
                "Passenger: Muhannad Albu Asais\nPNR: MUH123\n"
                "Ticket number: 065-2222222222\nFlight SV1671\n"
                "Flight date: 15 July 2026\nRUH to AHB"),
        },
    ]
    ingest(raw, log=lambda *_args, **_kwargs: None)
    telegram_bot.rebuild_flights(log=lambda *_args, **_kwargs: None)

    bot.handle_update({"message": {
        "message_id": 1902,
        "chat": {"id": 42},
        "text": (
            "/complaintref C_2788888 flight SV1671 date 2026-07-15 "
            "filed 2026-07-20 about damaged baggage"),
    }})

    assert db.list_complaints() == []
    assert "more than one possible family flight" in api.messages[-1]["text"]
