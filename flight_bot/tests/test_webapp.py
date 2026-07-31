from copy import deepcopy
from datetime import datetime

import pytest

from flight_bot import db, webapp
from flight_bot.config import DEFAULTS
from flight_bot.pipeline import load_demo
from flight_bot.parser import parse_email
from flight_bot.web_access import create_web_token


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    webapp._scan_progress.clear()
    config = deepcopy(DEFAULTS)
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    assert load_demo(log=lambda *args, **kwargs: None) == 3
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def test_primary_pages_render(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"FlightDeck" in response.data
    assert b"Your inbox, turned into answers" in response.data

    assert client.get("/emails").status_code == 200
    assert client.get("/gaca-cases").status_code == 200
    assert client.get("/healthz").json == {
        "status": "ok", "emails": 7, "flights": 3, "complaints": 0}

    flight_id = db.list_flights()[0]["id"]
    flight_page = client.get(f"/flight/{flight_id}")
    assert flight_page.status_code == 200
    assert b'<html lang="en" dir="ltr">' in flight_page.data
    assert b'<pre class="email-body" dir="auto">' in flight_page.data
    assert client.get(
        f"/flight/{flight_id}/complaint/airline").status_code == 200


def test_internal_sms_requires_secret_and_is_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    payload = {
        "sender": "SAUDIA", "received_at": "2026-07-19T22:30:00+03:00",
        "message_id": "shortcut-123",
        "text": "Your complaint reference number is 123456789",
    }
    assert client.post("/api/internal/sms", json=payload).status_code == 404
    first = client.post(
        "/api/internal/sms", json=payload,
        headers={"X-SMS-Secret": "shared-test-secret"})
    assert first.status_code == 200
    assert first.json["duplicate"] is False
    assert first.json["reference"] == "123456789"
    second = client.post(
        "/api/internal/sms", json=payload,
        headers={"X-SMS-Secret": "shared-test-secret"})
    assert second.json["duplicate"] is True
    assert len(db.list_sms_messages()) == 1
    event = db.list_mail_events()[0]
    assert event["sender"] == "sms@saudia.com"
    assert "123456789" in event["subject"]


def test_internal_sms_deduplicates_same_body_across_adjacent_minutes(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    headers = {"X-SMS-Secret": "shared-test-secret"}
    text = (
        "Dear Guest, we will review your comment ref. C_2778782 "
        "and respond as soon as possible."
    )

    first = client.post("/api/internal/sms", json={
        "sender": "Saudia",
        "received_at": "2026-07-30T16:45:00+03:00",
        "message_id": "shortcut-copy-a",
        "text": text,
    }, headers=headers)
    second = client.post("/api/internal/sms", json={
        "sender": "",
        "received_at": "2026-07-30T16:47:00+03:00",
        "message_id": "shortcut-copy-b",
        "text": text,
    }, headers=headers)

    assert first.json["duplicate"] is False
    assert second.json["duplicate"] is True
    assert second.json["duplicate_of_sms_id"]
    assert len(db.list_sms_messages()) == 1


def test_internal_sms_attaches_saudia_reference_to_flight_page(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    flight_key = "8GAKAN|SV1674|2026-07-09"
    db.replace_flights([{
        "flight_key": flight_key, "airline_code": "SV",
        "flight_number": "SV1674", "flight_numbers": ["SV1674"],
        "flight_date": "2026-07-09", "pnr": "8GAKAN", "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        flight_key, "airline", "Delayed and damaged baggage")
    db.finish_complaint(complaint_id, "accepted_pending_reference")

    response = client.post("/api/internal/sms", json={
        "sender": "Saudia", "received_at": "Jul 20, 2026 at 01:03",
        "message_id": "saudia-live-reference-1",
        "text": (
            "Dear Guest, Thank you for sharing your travel experience with us. "
            "We will review your comment ref. C_2778782 and respond as soon as "
            "possible. Guest Relations"
        ),
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.status_code == 200
    assert response.json["reference"] == "C_2778782"
    assert response.json["attached_complaint_id"] == complaint_id
    complaint = db.complaints_for_flight(flight_key)[0]
    assert complaint["status"] == "submitted"
    assert complaint["reference"] == "C_2778782"
    flight_id = db.get_flight_by_key(flight_key)["id"]
    page = client.get(f"/flight/{flight_id}")
    assert page.status_code == 200
    assert b"C_2778782" in page.data


def test_recovered_saudia_reference_clears_stale_retry_state(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    flight_key = "8GAKAN|SV1674|2026-07-09"
    db.replace_flights([{
        "flight_key": flight_key, "airline_code": "SV",
        "flight_number": "SV1674", "flight_numbers": ["SV1674"],
        "flight_date": "2026-07-09", "pnr": "8GAKAN", "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        flight_key, "airline", "Delayed and damaged baggage")
    db.finish_complaint(complaint_id, "accepted_pending_reference")
    db.save_portal_job({
        "id": "saudia-reference-recovery",
        "kind": "airline",
        "flight_key": flight_key,
        "complaint_id": complaint_id,
        "status": "accepted_pending_reference",
        "terminal": False,
        "next_attempt_at": 12345,
        "lease_until": 23456,
        "last_error": "stale pre-acceptance failure",
    })

    db.finish_complaint(
        complaint_id, "submitted", reference="C_2816389")

    job = db.get_portal_job("saudia-reference-recovery")
    assert job["status"] == "success"
    assert job["reference"] == "C_2816389"
    assert job["terminal"] == 1
    assert job["next_attempt_at"] is None
    assert job["lease_until"] is None
    assert job["last_error"] is None


@pytest.mark.parametrize(
    ("portal_status", "complaint_status"),
    [
        ("confirmation_unknown", "failed"),
        ("accepted_pending_reference", "accepted_pending_reference"),
    ],
)
def test_internal_sms_reconciles_sent_gaca_without_reference(
        tmp_path, monkeypatch, portal_status, complaint_status):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    flight_key = "8GAKAN|SV1674|2026-07-09"
    db.replace_flights([{
        "flight_key": flight_key, "airline_code": "SV",
        "flight_number": "SV1674", "flight_numbers": ["SV1674"],
        "flight_date": "2026-07-09", "pnr": "8GAKAN", "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        flight_key, "gaca", "Delayed and damaged baggage")
    db.finish_complaint(complaint_id, complaint_status)
    db.save_portal_job({
        "id": "gaca-unknown-job",
        "kind": "gaca",
        "flight_key": flight_key,
        "complaint_id": complaint_id,
        "status": portal_status,
        "terminal": True,
        "message": "Confirmation page blocked after one Submit.",
    })

    response = client.post("/api/internal/sms", json={
        "sender": "GACA",
        "received_at": "2026-07-25T06:01:00+03:00",
        "message_id": "gaca-reference-1",
        "text": (
            "GACA received your complaint for flight SV1674. "
            "Complaint reference number is GACA-483921."
        ),
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.status_code == 200
    assert response.json["reference"] == "GACA-483921"
    assert response.json["attached_complaint_id"] == complaint_id
    complaint = db.complaints_for_flight(flight_key)[0]
    assert complaint["status"] == "submitted"
    assert complaint["reference"] == "GACA-483921"
    job = db.get_portal_job("gaca-unknown-job")
    assert job["status"] == "submitted"
    assert job["reference"] == "GACA-483921"


def test_reconciled_sms_emits_one_final_notification(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"

    class Telegram:
        def __init__(self):
            self.final = []
            self.generic = []

        def notify_reference_reconciled(
                self, complaint_id, reference, *, source):
            self.final.append((complaint_id, reference, source))

        def notify(self, text):
            self.generic.append(text)

        def check_complaint_responses(self):
            raise AssertionError("Reference was expected to reconcile.")

    telegram = Telegram()
    monkeypatch.setattr(webapp, "start_telegram", lambda _config: telegram)
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    flight_key = "RXBOOKING|RX28|2026-06-16"
    db.replace_flights([{
        "flight_key": flight_key,
        "airline_code": "RX",
        "flight_number": "RX28",
        "flight_numbers": ["RX28"],
        "flight_date": "2026-06-16",
        "pnr": "RXBOOKING",
        "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        flight_key, "gaca", "On-board service complaint")
    db.finish_complaint(complaint_id, "accepted_pending_reference")
    db.save_portal_job({
        "id": "gaca-final-notification",
        "kind": "gaca",
        "flight_key": flight_key,
        "complaint_id": complaint_id,
        "status": "accepted_pending_reference",
        "terminal": True,
    })

    response = client.post("/api/internal/sms", json={
        "sender": "GACA CARE",
        "message_id": "gaca-final-reference",
        "text": "GACA تم استلام شكواكم C076574",
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.status_code == 200
    assert telegram.final == [(complaint_id, "C076574", "SMS")]
    assert telegram.generic == []


def test_gaca_sms_never_attaches_to_confirmed_pre_submit_failure(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    flight_key = "8GAKAN|SV1674|2026-07-09"
    db.replace_flights([{
        "flight_key": flight_key, "airline_code": "SV",
        "flight_number": "SV1674", "flight_numbers": ["SV1674"],
        "flight_date": "2026-07-09", "pnr": "8GAKAN", "email_ids": [],
    }])
    complaint_id = db.begin_complaint(
        flight_key, "gaca", "Delayed and damaged baggage")
    db.finish_complaint(complaint_id, "failed")
    db.save_portal_job({
        "id": "gaca-pre-submit-failure",
        "kind": "gaca",
        "flight_key": flight_key,
        "complaint_id": complaint_id,
        "status": "error",
        "terminal": True,
        "message": "Login failed before the form opened.",
    })

    response = client.post("/api/internal/sms", json={
        "sender": "GACA",
        "message_id": "gaca-reference-no-target",
        "text": "GACA complaint reference number is GACA-483921.",
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.status_code == 200
    assert response.json["attached_complaint_id"] is None
    complaint = db.complaints_for_flight(flight_key)[0]
    assert complaint["status"] == "failed"
    assert complaint["reference"] is None


def test_reference_only_gaca_sms_follows_portal_acceptance_order(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    first_key = "FIRST|SV1671|2026-07-05"
    second_key = "SECOND|SV1650|2026-07-17"
    db.replace_flights([
        {
            "flight_key": first_key, "airline_code": "SV",
            "flight_number": "SV1671", "flight_numbers": ["SV1671"],
            "flight_date": "2026-07-05", "pnr": "FIRST",
            "email_ids": [],
        },
        {
            "flight_key": second_key, "airline_code": "SV",
            "flight_number": "SV1650", "flight_numbers": ["SV1650"],
            "flight_date": "2026-07-17", "pnr": "SECOND",
            "email_ids": [],
        },
    ])
    # Create the second acceptance's complaint first to prove that complaint
    # ids and dashboard creation times cannot control the reference mapping.
    second_id = db.begin_complaint(second_key, "gaca", "Second acceptance")
    db.finish_complaint(second_id, "accepted_pending_reference")
    first_id = db.begin_complaint(first_key, "gaca", "First acceptance")
    db.finish_complaint(first_id, "accepted_pending_reference")
    db.save_portal_job({
        "id": "accepted-second", "kind": "gaca",
        "flight_key": second_key, "complaint_id": second_id,
        "status": "accepted_pending_reference", "terminal": True,
    })
    db.save_portal_job({
        "id": "accepted-first", "kind": "gaca",
        "flight_key": first_key, "complaint_id": first_id,
        "status": "accepted_pending_reference", "terminal": True,
    })
    with db.connect() as conn:
        conn.execute(
            "UPDATE portal_jobs SET updated_at=? WHERE id=?",
            ("2026-07-31 23:44:29", "accepted-second"),
        )
        conn.execute(
            "UPDATE portal_jobs SET updated_at=? WHERE id=?",
            ("2026-07-31 23:27:35", "accepted-first"),
        )

    first_response = client.post("/api/internal/sms", json={
        "sender": "GACA CARE", "message_id": "gaca-fifo-first",
        "text": "GACA received your complaint. Reference C076900.",
    }, headers={"X-SMS-Secret": "shared-test-secret"})
    second_response = client.post("/api/internal/sms", json={
        "sender": "GACA CARE", "message_id": "gaca-fifo-second",
        "text": "GACA received your complaint. Reference C076901.",
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert first_response.json["attached_complaint_id"] == first_id
    assert second_response.json["attached_complaint_id"] == second_id
    assert db.complaints_for_flight(first_key)[0]["reference"] == "C076900"
    assert db.complaints_for_flight(second_key)[0]["reference"] == "C076901"


def test_reference_only_sms_prefers_newest_pending_airline_complaint(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    older_key = "8GAKAN|SV1674|2026-07-09"
    newest_key = "7MZ63V|SV520|2025-12-06"
    db.replace_flights([
        {
            "flight_key": older_key, "airline_code": "SV",
            "flight_number": "SV1674", "flight_numbers": ["SV1674"],
            "flight_date": "2026-07-09", "pnr": "8GAKAN", "email_ids": [],
        },
        {
            "flight_key": newest_key, "airline_code": "SV",
            "flight_number": "SV520", "flight_numbers": ["SV520"],
            "flight_date": "2025-12-06", "pnr": "7MZ63V", "email_ids": [],
        },
    ])
    older_id = db.begin_complaint(older_key, "airline", "Older issue")
    db.finish_complaint(older_id, "accepted_pending_reference")
    newest_id = db.begin_complaint(newest_key, "airline", "Newest issue")
    db.finish_complaint(newest_id, "accepted_pending_reference")

    response = client.post("/api/internal/sms", json={
        "sender": "Saudia", "received_at": "Jul 20, 2026 at 01:03",
        "message_id": "saudia-newest-reference",
        "text": (
            "Dear Guest, we will review your comment ref. C_2778782 "
            "and respond as soon as possible."
        ),
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.status_code == 200
    assert response.json["attached_complaint_id"] == newest_id
    newest = db.complaints_for_flight(newest_key)[0]
    older = db.complaints_for_flight(older_key)[0]
    assert newest["status"] == "submitted"
    assert newest["reference"] == "C_2778782"
    assert older["status"] == "accepted_pending_reference"
    assert older["reference"] is None


def test_internal_sms_distills_otp_without_storing_body(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    headers = {"X-SMS-Secret": "shared-test-secret"}

    first = client.post("/api/internal/sms", json={
        "sender": "SAUDIA", "message_id": "otp-delivery-a",
        "text": "Your verification code is 4729",
    }, headers=headers)
    second = client.post("/api/internal/sms", json={
        "sender": "SAUDIA", "message_id": "otp-delivery-b",
        "text": "Your verification code is 4729",
    }, headers=headers)

    assert first.json == {
        "consumed": False, "duplicate": False, "ignored": True, "ok": True,
        "otp": "4729", "reason": "otp",
    }
    assert second.json["duplicate"] is True
    assert db.list_sms_messages() == []
    assert db.list_mail_events() == []


def test_gaca_c_prefix_reference_requires_gaca_context():
    body = "Your complaint C076100 has been received."
    assert webapp._extract_sms_reference(body, "GACA CARE") == "C076100"
    assert webapp._extract_sms_reference(body, "SAMA") == ""


def test_internal_sms_ignores_non_aviation_shortcut_traffic(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    config = deepcopy(DEFAULTS)
    config["sms"]["ingest_secret"] = "shared-test-secret"
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()

    response = client.post("/api/internal/sms", json={
        "sender": "SAMA",
        "message_id": "sama-bank-complaint",
        "text": (
            "تم استلام اعتراضكم حيال معالجة البنك للشكوى رقم "
            "C2607056702 وستتم مراجعة الشكوى."
        ),
    }, headers={"X-SMS-Secret": "shared-test-secret"})

    assert response.json == {
        "ignored": True,
        "ok": True,
        "reason": "non-aviation",
    }
    assert db.list_sms_messages() == []
    assert db.list_mail_events() == []


def test_generic_government_service_journey_is_not_an_aviation_sms():
    assert not webapp._is_aviation_sms(
        "Riyadh 940",
        "نشكرك على تقييم رحلتك في خدمة بلاغات مدينة الرياض.",
    )


def test_manual_corrections_are_validated_and_saved(client):
    flight_id = db.list_flights()[0]["id"]
    bad = client.post(f"/flight/{flight_id}/override", data={"origin": "R"})
    assert bad.status_code == 302
    assert not db.get_flight(flight_id)["overrides"].get("origin")

    response = client.post(f"/flight/{flight_id}/override", data={
        "flight_number": "sv 900", "flight_date": "2026-06-01",
        "origin": "ruh", "destination": "jed",
        "arrival": "2026-06-01T12:00",
        "actual_arrival": "2026-06-01T16:15",
        "accepted_alternative": "no", "cancelled": "no",
    })
    assert response.status_code == 302
    saved = db.get_flight(flight_id)["overrides"]
    assert saved["origin"] == "RUH"
    assert saved["destination"] == "JED"
    assert saved["actual_arrival"] == "2026-06-01 16:15"


def test_official_portal_route_uses_only_the_incident(client, monkeypatch):
    launched = {}
    launches = []

    def fake_start(payload, on_complete, on_update=None):
        launches.append(payload)
        launched.update(payload)
        on_complete(webapp.PortalResult(
            "submitted", "Submitted.", "CASE-123456"))
        return "job-123"

    monkeypatch.setattr(webapp, "missing_portal_fields", lambda payload: [])
    monkeypatch.setattr(webapp, "start_portal_job", fake_start)
    flight_id = db.list_flights()[0]["id"]
    page = client.get(f"/flight/{flight_id}/complaint/airline")
    assert b"What went wrong?" in page.data
    assert b"Email and SMTP are not used" in page.data
    assert b"Recipient" not in page.data
    assert b"Open email app" not in page.data

    response = client.post(f"/flight/{flight_id}/complaint/airline/submit", data={
        "incident": "The flight was cancelled and I had to buy a hotel room.",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/complaints/jobs/job-123?flight_id=" + str(flight_id))
    assert launched["kind"] == "airline"
    assert launched["incident"] == (
        "The flight was cancelled and I had to buy a hotel room.")
    assert "to" not in launched
    assert "smtp" not in launched
    flight = db.get_flight(flight_id)
    assert flight["complaints"][0]["status"] == "submitted"
    assert flight["complaints"][0]["reference"] == "CASE-123456"

    duplicate = client.post(
        f"/flight/{flight_id}/complaint/airline/submit", data={
            "incident": "The flight was cancelled and I had to buy a hotel room.",
        }, follow_redirects=True)
    assert duplicate.status_code == 200
    assert b"will not submit it again" in duplicate.data
    assert len(launches) == 1
    assert len(db.complaints_for_flight(flight["flight_key"])) == 1


def test_gaca_requires_and_reuses_airline_reference(client):
    flight = db.list_flights()[0]
    blocked = client.post(
        f"/flight/{flight['id']}/complaint/gaca/submit",
        data={"incident": "The airline did not resolve my cancelled flight."},
        follow_redirects=True)
    assert b"Submit to the airline first" in blocked.data

    db.add_complaint(
        flight["flight_key"], "airline", None, "Airline complaint",
        "submitted", reference="CAS-998877",
        details="The airline cancelled my flight without suitable care.")
    page = client.get(f"/flight/{flight['id']}/complaint/gaca")
    assert b"CAS-998877" in page.data
    assert b"cancelled my flight without suitable care" in page.data


def test_profile_is_saved_once_and_returns_to_claim(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(webapp, "save_user_profile", saved.update)
    response = client.post("/settings/profile", data={
        "next": "/", "first_name": "Test", "middle_name": "Middle",
        "last_name": "Passenger", "email": "p@example.com",
        "phone": "+966500000000", "national_id": "ID123456",
        "title": "Mr", "nationality": "Saudi Arabian",
        "country_code": "+966", "alfursan_id": "30680000",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert saved["full_name"] == "Test Middle Passenger"
    assert saved["last_name"] == "Passenger"
    assert saved["alfursan_id"] == "30680000"
    assert saved["country_code"] == "+966"


def test_family_passenger_gets_separate_profile_and_claim_is_blocked_until_saved(
        client, monkeypatch):
    flight = db.list_flights()[0]
    db.set_overrides(flight["id"], {"passenger": "Muhannad Alqahtani"})
    claim_url = f"/flight/{flight['id']}/complaint/airline"

    page = client.get(claim_url)
    assert b"saved identity profile for Muhannad Alqahtani" in page.data
    assert b"Complete Muhannad Alqahtani" in page.data

    saved = {}
    monkeypatch.setattr(
        webapp, "save_passenger_profile",
        lambda passenger, values: saved.update(passenger=passenger, **values))
    response = client.post("/settings/profile", data={
        "next": claim_url, "passenger": "Muhannad Alqahtani",
        "first_name": "Muhannad", "middle_name": "",
        "last_name": "Alqahtani", "email": "m@example.com",
        "phone": "+966511111111", "national_id": "MUHANNAD-ID",
        "title": "Mr", "nationality": "Saudi Arabian",
        "country_code": "+966", "alfursan_id": "FAMILY-123",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith(claim_url)
    assert saved["passenger"] == "Muhannad Alqahtani"
    assert saved["national_id"] == "MUHANNAD-ID"
    assert saved["alfursan_id"] == "FAMILY-123"

    updated = client.get(claim_url)
    assert b"saved identity profile for Muhannad Alqahtani" not in updated.data


def test_family_profile_is_prefilled_from_matching_ticket_evidence(client):
    parsed = parse_email(
        "<family-identity@example>", "Your Saudia e-ticket SV1650",
        "noreply@saudia.com", datetime(2026, 7, 15, 12, 0),
        """Booking reference: ABC123
        Flight SV1650 - JED to AHB
        Mr Muhannad Alqahtani e-Ticket: 065-2200741431
        Frequent Flyer: 30681234 National ID: 1122334455
        """)
    db.save_email(parsed)

    page = client.get(
        "/settings/profile?passenger=Muhannad%20Alqahtani&next=/")
    assert page.status_code == 200
    assert b"Auto-filled from matching travel evidence" in page.data
    assert b'value="30681234"' in page.data
    assert b'value="1122334455"' in page.data
    assert b'<option value="Mr" selected' in page.data


def test_anthropic_profile_fallback_is_on_demand_grounded_and_cached(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ai-profile.db")
    calls = []

    class Assistant:
        enabled = True
        name = "Ghala"
        model = "claude-test"
        last_error = ""
        settings = {
            "extract_profile_evidence": True,
            "max_portal_attempts": 3,
        }

        def __init__(self, _config):
            pass

        def portal_decision(self, _challenge):
            return None

        def extract_passenger_profile(self, passenger, evidence):
            calls.append((passenger, evidence))
            return {
                "values": {"nationality": "Saudi"},
                "evidence": {"nationality": "Ghala verified in ticket"},
            }

    monkeypatch.setattr(webapp, "ClaudeAssistant", Assistant)
    config = deepcopy(DEFAULTS)
    config["ai"].update({"enabled": True, "api_key": "test-key"})
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    parsed = parse_email(
        "<ai-family-profile@example>", "Your Saudia e-ticket SV1650",
        "noreply@saudia.com", datetime(2026, 7, 15, 12, 0),
        """Booking reference: ABC123
        Flight SV1650 - JED to AHB
        Mr Muhannad Alqahtani e-Ticket: 065-2200741431
        Citizenship: Saudi
        """)
    db.save_email(parsed)
    client = application.test_client()

    page = client.get(
        "/settings/profile?passenger=Muhannad%20Alqahtani&next=/")
    assert b"Ghala is checking" in page.data
    first = client.post("/settings/profile/ai-suggestions", data={
        "passenger": "Muhannad Alqahtani", "fields": "nationality",
    })
    second = client.post("/settings/profile/ai-suggestions", data={
        "passenger": "Muhannad Alqahtani", "fields": "nationality",
    })
    assert first.json["values"] == {"nationality": "Saudi"}
    assert first.json["cached"] is False
    assert second.json["cached"] is True
    assert len(calls) == 1
    assert "Lujain" not in calls[0][1][0]["text"]


def test_scan_without_credentials_gives_actionable_message(client):
    response = client.post("/scan", follow_redirects=True)
    assert response.status_code == 200
    assert b"app password" in response.data
    assert not webapp._scan_running()


def test_remote_dashboard_requires_telegram_link(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "protected.db")
    config = deepcopy(DEFAULTS)
    config["web"].update({
        "public_base_url": "http://flightdeck.example:5000",
        "access_secret": "private-test-secret",
        "link_expiry_minutes": 15,
    })
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    client = application.test_client()
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200

    token = create_web_token("private-test-secret", "42")
    signed_in = client.get(f"/?access={token}")
    assert signed_in.status_code == 302
    assert "access=" not in signed_in.headers["Location"]
    assert client.get("/").status_code == 200


def test_mobile_complaint_announces_telegram_screenshot_assistance(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mobile.db")
    notices = []

    class Telegram:
        def notify(self, message, **_kwargs):
            notices.append(message)

        def portal_progress_handler(self):
            return None

    monkeypatch.setattr(webapp, "start_telegram", lambda _config: Telegram())
    monkeypatch.setattr(webapp, "missing_portal_fields", lambda _payload: [])
    monkeypatch.setattr(
        webapp, "start_portal_job",
        lambda _payload, on_complete, on_update=None: "mobile-job")
    config = deepcopy(DEFAULTS)
    config["user"].update({
        "full_name": "Mobile Passenger", "email": "p@example.com",
        "phone": "+966500000000", "national_id": "ID123456",
        "title": "Mr", "nationality": "Saudi Arabian",
        "country_code": "+966",
    })
    application = webapp.create_app(config)
    application.config.update(TESTING=True)
    load_demo(log=lambda *_args, **_kwargs: None)
    flight_id = db.list_flights()[0]["id"]
    response = application.test_client().post(
        f"/flight/{flight_id}/complaint/airline/submit",
        data={"incident": "The seat and entertainment screen were broken."})
    assert response.status_code == 302
    assert any("screenshot" in notice and "Telegram" in notice
               for notice in notices)
