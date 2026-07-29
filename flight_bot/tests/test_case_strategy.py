from datetime import datetime, timedelta

from flight_bot.case_strategy import incident_category, recommend_case


def flight():
    return {
        "flight_key": "sv100-2026-07-14",
        "airline_code": "SV", "airline_name": "Saudia",
        "flight_number": "SV100", "flight_date": "2026-07-14",
        "origin": "RUH", "destination": "JED",
        "departure": "2026-07-14 10:00", "arrival": "2026-07-14 12:00",
        "passenger": "Mansour Albu Asais", "overrides": {},
    }


def test_plain_code_categorizes_baggage_without_ai():
    assert incident_category("My baggage arrived damaged") == "baggage"
    assert incident_category("The seat screen did not work") == "entertainment"


def test_airborne_case_waits_for_final_arrival_before_delay_claim():
    strategy = recommend_case(
        flight(), {"status": "airborne", "label": "Airborne",
                   "provider": "airplanes_live", "confidence": .82},
        complaints=[])
    assert strategy["recommended_action"] == "wait_for_arrival"
    assert "arrival" in strategy["reasons"][0].lower()


def test_referenced_airline_case_becomes_gaca_ready_after_seven_days():
    created = datetime(2026, 7, 1, 10, 0)
    complaints = [{
        "id": 4, "kind": "airline", "status": "submitted",
        "reference": "C_2761389", "created_at": created.isoformat(" "),
        "details": "The seat screen was broken for the entire flight.",
        "attachments": [],
    }]
    strategy = recommend_case(
        flight(), {"status": "landed", "label": "Landed",
                   "provider": "flightaware", "confidence": .95},
        complaints=complaints, now=created + timedelta(days=8))
    assert strategy["recommended_action"] == "escalate_gaca"
    assert strategy["filing_deadline"] == "2026-09-12"


def test_missing_airline_reference_blocks_regulator_escalation():
    created = datetime(2026, 7, 1, 10, 0)
    strategy = recommend_case(
        flight(), {"status": "landed", "provider": "flightaware",
                   "confidence": .95},
        complaints=[{
            "id": 5, "kind": "airline", "status": "submitted",
            "reference": "", "created_at": created.isoformat(" "),
            "details": "The seat screen was broken for the entire flight.",
        }], now=created + timedelta(days=8))
    assert strategy["recommended_action"] == "recover_airline_reference"


def test_closed_airline_complaint_recommends_reopen_or_gaca():
    created = datetime(2026, 7, 1, 10, 0)
    strategy = recommend_case(
        flight(), {"status": "landed", "provider": "flightaware",
                   "confidence": .95},
        complaints=[{
            "id": 13, "kind": "airline", "status": "closed",
            "reference": "C_2781202", "created_at": created.isoformat(" "),
            "details": "The seat screen was broken.",
        }],
        responses=[{"mail_event_id": 1, "match_method": "exact_reference"}],
        now=created + timedelta(days=2))
    assert strategy["recommended_action"] == "escalate_gaca"
