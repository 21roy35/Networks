from flight_bot import db
from flight_bot.gaca_account import (
    import_gaca_cases,
    map_gaca_case,
    normalize_gaca_case,
)


def _flight(key, number, travel_date, pnr, ticket, passenger):
    return {
        "flight_key": key,
        "airline_code": "SV",
        "airline_name": "Saudia",
        "flight_number": number,
        "flight_numbers": [number],
        "flight_date": travel_date,
        "pnr": pnr,
        "ticket_numbers": [ticket],
        "passenger": passenger,
        "origin": "RUH",
        "destination": "JED",
        "overrides": {},
    }


def test_normalize_gaca_detail_extracts_regulator_and_airline_facts():
    case = normalize_gaca_case({
        "url": "https://myeservices.gaca.gov.sa/eservices/account/request/9",
        "title": "Complaint details",
        "text": "Complaint C076239 for flight SV 1671",
        "pairs": [
            {"label": "Complaint Number", "value": "C076239"},
            {"label": "Complaint status", "value": "Under review"},
            {"label": "Airline", "value": "SAUDIA"},
            {"label": "Airline complaint reference", "value": "C_2760788"},
            {"label": "Flight Number", "value": "SV 1671"},
            {"label": "Flight Date", "value": "2026-06-15"},
            {"label": "E-ticket number", "value": "0652200278935"},
            {"label": "Booking Reference", "value": "7V5F9V"},
            {"label": "Passenger Name", "value": "Mansour Albu Asais"},
        ],
    })

    assert case["reference"] == "C076239"
    assert case["airline_reference"] == "C_2760788"
    assert case["flight_number"] == "SV1671"
    assert case["flight_date"] == "2026-06-15"
    assert case["ticket_number"] == "0652200278935"
    assert case["pnr"] == "7V5F9V"
    assert case["passenger_name"] == "Mansour Albu Asais"


def test_public_information_page_is_not_imported_as_a_case():
    assert normalize_gaca_case({
        "url": "https://myeservices.gaca.gov.sa/eservices/about",
        "title": "About",
        "text": (
            "No. 12 of 2024. General aviation services are available "
            "through the public website."),
        "pairs": [],
    }) is None


def test_mapper_uses_airline_reference_before_shared_family_contact_data():
    mansour = _flight(
        "mansour-flight", "SV1671", "2026-06-15", "7V5F9V",
        "0652200278935", "Mansour Albu Asais")
    muhannad = _flight(
        "muhannad-flight", "SV1678", "2026-06-18", "M7X2AA",
        "0652200999123", "Muhannad Albu Asais")
    complaints = [
        {
            "id": 10, "kind": "airline", "flight_key": "mansour-flight",
            "reference": "C_2760788", "flight_data": mansour,
        },
        {
            "id": 11, "kind": "gaca", "flight_key": "mansour-flight",
            "parent_complaint_id": 10, "reference": "",
            "flight_data": mansour,
        },
        {
            "id": 20, "kind": "airline", "flight_key": "muhannad-flight",
            "reference": "C_2761389", "flight_data": muhannad,
        },
        {
            "id": 21, "kind": "gaca", "flight_key": "muhannad-flight",
            "parent_complaint_id": 20, "reference": "",
            "flight_data": muhannad,
        },
    ]

    mapping = map_gaca_case({
        "reference": "C076239",
        "airline_reference": "C_2761389",
        "flight_number": "SV1678",
        "flight_date": "2026-06-18",
        "passenger_name": "Muhannad Albu Asais",
    }, complaints, [mansour, muhannad])

    assert mapping["status"] == "mapped"
    assert mapping["complaint_id"] == 21
    assert mapping["flight_key"] == "muhannad-flight"
    assert "airline_reference" in mapping["method"]


def test_mapper_refuses_tied_flight_number_without_stronger_evidence():
    first = _flight(
        "first", "SV100", "2026-06-01", "AAA111",
        "0651111111111", "First Passenger")
    second = _flight(
        "second", "SV100", "2026-06-01", "BBB222",
        "0652222222222", "Second Passenger")

    mapping = map_gaca_case({
        "reference": "C077777",
        "flight_number": "SV100",
        "flight_date": "2026-06-01",
    }, [], [first, second])

    assert mapping["status"] == "ambiguous"
    assert mapping["flight_key"] == ""


def test_account_import_reconciles_held_job_authoritatively(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    flight = _flight(
        "sv1671-flight", "SV1671", "2026-06-15", "7V5F9V",
        "0652200278935", "Mansour Albu Asais")
    db.replace_flights([{**flight, "email_ids": []}])
    airline_id = db.begin_complaint(
        flight["flight_key"], "airline", "Broken screen")
    db.finish_complaint(airline_id, "submitted", "C_2760788")
    gaca_id = db.begin_complaint(
        flight["flight_key"], "gaca", "Escalation",
        parent_complaint_id=airline_id)
    db.save_portal_job({
        "id": "held-gaca-case",
        "kind": "gaca",
        "flight_key": flight["flight_key"],
        "complaint_id": gaca_id,
        "status": "held",
        "message": "Portal reported duplicate",
        "terminal": False,
    })

    result = import_gaca_cases([{
        "case_key": "C076239",
        "reference": "C076239",
        "status": "Under review",
        "airline": "Saudia",
        "airline_reference": "C_2760788",
        "flight_number": "SV1671",
        "flight_date": "2026-06-15",
        "ticket_number": "0652200278935",
        "pnr": "7V5F9V",
        "passenger_name": "Mansour Albu Asais",
        "source_url": "https://myeservices.gaca.gov.sa/eservices/request/1",
        "raw": {},
    }])

    assert result.cases_reconciled == 1
    complaint = db.get_complaint(gaca_id)
    assert complaint["status"] == "submitted"
    assert complaint["reference"] == "C076239"
    job = db.get_portal_job("held-gaca-case")
    assert job["status"] == "submitted"
    assert job["terminal"] == 1
    imported = db.list_gaca_account_cases()
    assert imported[0]["mapping_status"] == "reconciled"
    assert imported[0]["mapped_complaint_id"] == gaca_id
