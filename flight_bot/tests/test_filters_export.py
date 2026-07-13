from copy import deepcopy

import pytest

from flight_bot import db
from flight_bot.config import DEFAULTS
from flight_bot.webapp import (_filter_flights, _flight_view, _flights_as_text,
                               _payment_card, create_app)


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


def test_payment_card_keeps_brand_and_drops_masked_digits():
    assert _payment_card("Visa •••• 1234") == "Visa"
    assert _payment_card("Mastercard") == "Mastercard"
    assert _payment_card("") == ""


def test_passenger_and_card_filters_compose_exactly():
    flights = [_flight_view(flight) for flight in (
        sample_flight("1", "SV101", "Alice Example", "Visa •••• 1111"),
        sample_flight("2", "SV202", "Bob Example", "Mastercard"),
        sample_flight("3", "SV303", "Alice Example", "Mastercard"),
    )]
    filtered, status = _filter_flights(
        flights, passenger="Alice Example", card="Mastercard")
    assert status == "all"
    assert [flight["display_flight_number"] for flight in filtered] == ["SV303"]


def test_text_export_contains_all_key_details_for_only_filtered_results():
    flights = [_flight_view(sample_flight(
        "3", "SV303", "Alice Example", "Mastercard •••• 4242"))]
    output = _flights_as_text(flights, {
        "query": "", "status": "all",
        "passenger": "Alice Example", "card": "Mastercard",
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
        sample_flight("2", "SV202", "Bob Example", "Mastercard"),
        sample_flight("3", "SV303", "Alice Example", "Mastercard"),
    ])
    return app.test_client()


def test_dashboard_and_txt_endpoint_use_the_same_filters(filtered_client):
    query = "?passenger=Alice%20Example&card=Mastercard"
    page = filtered_client.get("/" + query)
    assert page.status_code == 200
    assert b"SV303" in page.data
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
        "/flights/export.txt?passenger=Corrected%20Passenger&card=American%20Express")
    assert "SV101" in exported.get_data(as_text=True)
