from copy import deepcopy
from datetime import datetime

import pytest

from flight_bot import db
from flight_bot.config import DEFAULTS
from flight_bot.webapp import (_filter_flights, _flight_view, _flights_as_text,
                               _payment_card, create_app)
from flight_bot.parser import parse_email


def sample_flight(key, number, passenger, payment):
    return {
        "flight_key": key,
        "airline_code": "SV",
        "airline_name": "Saudia",
        "flight_number": number,
        "flight_numbers": [number],
        "flight_date": "2026-06-01",
        "origin": "RUH",
        "destination": "JED",
        "pnr": f"PNR{key}",
        "passenger": passenger,
        "payment_method": payment,
        "amount": "450.00",
        "currency": "SAR",
        "email_count": 2,
        "kind_labels": ["Booking confirmation", "Receipt"],
        "email_ids": [],
    }


def test_payment_card_keeps_brand_and_last_four_only():
    assert _payment_card("Visa •••• 1234") == "Visa •••• 1234"
    assert _payment_card("AMEX ending in 9000") == "American Express •••• 9000"
    assert _payment_card("•••• 5678") == "Card •••• 5678"
    assert _payment_card("Visa 4111111111111234") == "Visa •••• 1234"
    assert _payment_card("Mastercard") == "Mastercard"
    assert _payment_card("") == ""


@pytest.mark.parametrize("payment_line, expected", [
    ("Paid with Visa ending in 4242", "Visa •••• 4242"),
    ("Payment: MADA ****9911", "MADA •••• 9911"),
    ("Master Card last 4 digits: 7788", "Mastercard •••• 7788"),
])
def test_email_parser_extracts_card_last_four(payment_line, expected):
    parsed = parse_email(
        "<payment-test>", "Booking confirmed ABC123", "booking@saudia.com",
        datetime(2026, 7, 14, 10, 0),
        f"Passenger: Test Passenger\nFlight SV101 RUH to JED\n{payment_line}")
    assert parsed is not None
    assert parsed.payment_method == expected


def test_passenger_and_card_filters_compose_exactly():
    flights = [_flight_view(flight) for flight in (
        sample_flight("1", "SV101", "Alice Example", "Visa •••• 1111"),
        sample_flight("2", "SV202", "Bob Example", "Mastercard •••• 2222"),
        sample_flight("3", "SV303", "Alice Example", "Mastercard •••• 3333"),
    )]
    filtered, status = _filter_flights(
        flights, passenger="Alice Example", card="Mastercard •••• 3333")
    assert status == "all"
    assert [flight["display_flight_number"] for flight in filtered] == ["SV303"]


def test_text_export_contains_all_key_details_for_only_filtered_results():
    flights = [_flight_view(sample_flight(
        "3", "SV303", "Alice Example", "Mastercard •••• 4242"))]
    output = _flights_as_text(flights, {
        "query": "", "status": "all",
        "passenger": "Alice Example", "card": "Mastercard •••• 4242",
    })
    assert "Results: 1" in output
    assert "Passenger: Alice Example" in output
    assert "Payment method: Mastercard •••• 4242" in output
    assert "Ticket price: SAR 450.00" in output
    assert "Source emails: 2" in output


@pytest.fixture()
def filtered_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "filters.db")
    app = create_app(deepcopy(DEFAULTS))
    app.config.update(TESTING=True)
    db.replace_flights([
        sample_flight("1", "SV101", "Alice Example", "Visa •••• 1111"),
        sample_flight("2", "SV202", "Bob Example", "Mastercard •••• 2222"),
        sample_flight("3", "SV303", "Alice Example", "Mastercard •••• 3333"),
    ])
    return app.test_client()


def test_dashboard_and_txt_endpoint_use_the_same_filters(filtered_client):
    query = "?passenger=Alice%20Example&card=Mastercard%20%E2%80%A2%E2%80%A2%E2%80%A2%E2%80%A2%203333"
    page = filtered_client.get("/" + query)
    assert page.status_code == 200
    assert b"SV303" in page.data
    assert "Mastercard •••• 3333" in page.get_data(as_text=True)
    assert b"SV101" not in page.data
    assert b"SV202" not in page.data
    assert b"Copy filtered TXT" in page.data

    exported = filtered_client.get("/flights/export.txt" + query)
    assert exported.status_code == 200
    assert exported.mimetype == "text/plain"
    assert "attachment; filename=" in exported.headers["Content-Disposition"]
    text = exported.get_data(as_text=True)
    assert "SV303" in text
    assert "SV101" not in text
    assert "SV202" not in text


def test_manual_passenger_and_card_corrections_feed_filters(filtered_client):
    flight_id = next(flight["id"] for flight in db.list_flights()
                     if flight["flight_number"] == "SV101")
    response = filtered_client.post(f"/flight/{flight_id}/override", data={
        "flight_number": "SV101", "flight_date": "2026-06-01",
        "origin": "RUH", "destination": "JED",
        "passenger": "Corrected Passenger",
        "payment_method": "American Express •••• 9000",
    })
    assert response.status_code == 302
    corrected = db.get_flight(flight_id)["overrides"]
    assert corrected["passenger"] == "Corrected Passenger"
    assert corrected["payment_method"] == "American Express •••• 9000"

    exported = filtered_client.get(
        "/flights/export.txt?passenger=Corrected%20Passenger&card=American%20Express%20%E2%80%A2%E2%80%A2%E2%80%A2%E2%80%A2%209000")
    assert "SV101" in exported.get_data(as_text=True)


def test_flight_page_never_displays_more_than_last_four(filtered_client):
    flight_id = db.list_flights()[0]["id"]
    db.set_override(flight_id, "payment_method", "Visa 4111111111111234")
    page = filtered_client.get(f"/flight/{flight_id}").get_data(as_text=True)
    assert "4111111111111234" not in page
    assert "Visa •••• 1234" in page
