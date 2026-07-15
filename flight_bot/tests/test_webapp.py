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
    assert client.get("/healthz").json == {
        "status": "ok", "emails": 7, "flights": 3, "complaints": 0}

    flight_id = db.list_flights()[0]["id"]
    assert client.get(f"/flight/{flight_id}").status_code == 200
    assert client.get(
        f"/flight/{flight_id}/complaint/airline").status_code == 200


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
