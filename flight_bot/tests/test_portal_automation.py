import pytest

from flight_bot.complaints import complaint_payload, missing_portal_fields
from flight_bot.portal_automation import (_extract_reference,
                                          _extract_reference_from_url, _is_official_url)


def sample_flight():
    return {
        "airline_code": "SV",
        "airline_name": "Saudia",
        "passenger": "Test Passenger",
        "pnr": "ABC123",
        "ticket_numbers": ["065-1234567890"],
        "flight_number": "SV 100",
        "flight_date": "2026-06-01",
        "origin": "RUH",
        "destination": "JED",
        "departure": "2026-06-01 10:30",
        "emails": [],
        "overrides": {},
    }


def profile():
    return {
        "full_name": "Test Passenger",
        "email": "passenger@example.com",
        "phone": "+966500000000",
        "national_id": "ID123456",
        "title": "Mr",
        "nationality": "Saudi Arabian",
        "country_code": "+966",
    }


def test_payload_maps_incident_and_every_known_portal_field():
    payload = complaint_payload(
        sample_flight(), profile(), "airline",
        "My flight was cancelled without a suitable alternative.")
    assert payload["first_name"] == "Test"
    assert payload["last_name"] == "Passenger"
    assert payload["ticket_number"] == "065-1234567890"
    assert payload["route"] == "RUH → JED"
    assert payload["incident"] in payload["description"]
    assert missing_portal_fields(payload) == []


def test_gaca_payload_cannot_skip_the_airline_reference():
    with pytest.raises(ValueError, match="airline first"):
        complaint_payload(
            sample_flight(), profile(), "gaca",
            "The carrier did not resolve the cancelled flight.")


def test_only_official_registry_hosts_are_accepted():
    assert _is_official_url("https://help.flynas.com/en")
    assert _is_official_url("https://myeservices.gaca.gov.sa/service")
    assert not _is_official_url("https://gaca.gov.sa.evil.example/claim")
    assert not _is_official_url("https://example.com/claim")


def test_reference_is_extracted_from_official_confirmation_text():
    assert _extract_reference(
        "Thank you. Your complaint reference number is CAS-12345678.") == (
            "CAS-12345678")


def test_reference_is_extracted_from_confirmation_url():
    assert _extract_reference_from_url(
        "https://help.flyadeal.com/hc/en-us/requests/123456") == "123456"
