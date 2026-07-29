from datetime import datetime

from flight_bot import db
from flight_bot.pipeline import rebuild_flights
from flight_bot.ticket_import import (
    build_parsed_ticket,
    deterministic_ticket_details,
    manual_complaint_from_text,
    missing_ticket_fields,
)


def test_deterministic_ticket_import_keeps_passenger_and_manual_case_facts():
    source = """
    Add this ticket
    Airline: Saudia
    Passenger: Muhannad Albu Asais
    PNR: ABC123
    E-ticket number: 065-1234567890
    Flight number: SV520
    Flight date: 2026-07-15
    Route: RUH to BAH
    Departure: 2026-07-15 10:15
    Arrival: 2026-07-15 11:30
    Manual complaint C_2779999 filed 2026-07-20
    about the seat screen was broken.
    """

    details = deterministic_ticket_details(source)

    assert missing_ticket_fields(details) == []
    assert details["passenger"] == "Muhannad Albu Asais"
    assert details["pnr"] == "ABC123"
    assert details["ticket_numbers"] == ["065-1234567890"]
    assert details["segments"] == [{
        "flight_number": "SV520",
        "flight_date": "2026-07-15",
        "origin": "RUH",
        "destination": "BAH",
        "departure": "2026-07-15 10:15",
        "arrival": "2026-07-15 11:30",
    }]
    assert details["complaint"]["reference"] == "C_2779999"
    assert details["complaint"]["filed_at"] == "2026-07-20"
    assert "screen was broken" in details["complaint"]["text"]


def test_manual_reference_parser_does_not_treat_eticket_as_case_number():
    value = manual_complaint_from_text(
        "/complaintref C_2787654 flight SV1671 date 2026-07-10 "
        "ticket 065-1234567890 filed 2026-07-12 about damaged baggage")

    assert value["reference"] == "C_2787654"
    assert value["flight_number"] == "SV1671"
    assert value["filed_at"] == "2026-07-12"


def test_synthetic_telegram_ticket_survives_normal_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    details = {
        "airline_code": "SV",
        "airline_name": "Saudia",
        "pnr": "FAMILY7",
        "ticket_numbers": ["065-1234567890"],
        "passenger": "Muhannad Albu Asais",
        "segments": [
            {
                "flight_number": "SV520",
                "flight_date": "2026-07-15",
                "origin": "RUH",
                "destination": "BAH",
                "departure": "2026-07-15 10:15",
                "arrival": "2026-07-15 11:30",
            },
            {
                "flight_number": "SV521",
                "flight_date": "2026-07-20",
                "origin": "BAH",
                "destination": "RUH",
                "departure": "2026-07-20 18:00",
                "arrival": "2026-07-20 19:15",
            },
        ],
    }
    parsed = build_parsed_ticket(
        details,
        message_id="telegram-ticket:42:abc",
        imported_at=datetime(2026, 7, 21, 9, 0),
        source_text="Original Telegram ticket text",
    )
    db.save_email(parsed)

    assert rebuild_flights(log=lambda *_args: None) == 2
    flights = db.list_flights()
    assert {item["flight_number"] for item in flights} == {"SV520", "SV521"}
    assert {item["passenger"] for item in flights} == {
        "Muhannad Albu Asais"}
    # A second global rebuild is what later Gmail scans/reparses rely on.
    assert rebuild_flights(log=lambda *_args: None) == 2


def test_manual_complaint_is_provenanced_and_idempotent(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    first, outcome = db.record_manual_airline_complaint(
        "ABC123|SV520|2026-07-15",
        "C_2779999",
        details="The seat screen was broken.",
        filed_at="2026-07-20",
    )
    second, repeated = db.record_manual_airline_complaint(
        "ABC123|SV520|2026-07-15",
        "C_2779999",
        details="The seat screen was broken.",
        filed_at="2026-07-20",
    )

    assert outcome == "created"
    assert repeated == "already_linked"
    assert first["id"] == second["id"]
    assert first["submission_source"] == "manual_telegram"
    assert first["created_at"] == "2026-07-20 00:00:00"
    assert len(db.list_complaints()) == 1
