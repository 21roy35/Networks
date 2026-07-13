from flight_bot.compensation import (ELIGIBLE, NOT_ELIGIBLE, POSSIBLY,
                                     UNKNOWN, assess)


def base_flight(**overrides):
    flight = {
        "airline_code": "SV",
        "origin": "RUH",
        "destination": "JED",
        "flight_date": "2026-06-01",
        "departure": "2026-06-01 10:00",
        "arrival": "2026-06-01 11:30",
        "cancelled": False,
        "overrides": {},
    }
    flight.update(overrides)
    return flight


def test_gaca_three_to_six_hour_delay_uses_50_sdr():
    result = assess(base_flight(
        overrides={"actual_arrival": "2026-06-01 15:30"}))
    assert result["verdict"] == ELIGIBLE
    assert "50 Special Drawing Rights (GACA)" in result["remedies"]


def test_gaca_over_six_hour_delay_uses_150_sdr():
    result = assess(base_flight(
        overrides={"actual_arrival": "2026-06-01 18:00"}))
    assert result["verdict"] == ELIGIBLE
    assert "150 Special Drawing Rights (GACA)" in result["remedies"]


def test_gaca_cancellation_band_uses_notice_and_alternative_choice():
    result = assess(base_flight(
        cancelled=True,
        overrides={"cancellation_notice_days": 10,
                   "accepted_alternative": "no"},
    ))
    assert result["verdict"] == ELIGIBLE
    assert any("75%" in remedy for remedy in result["remedies"])


def test_gaca_cancellation_is_conservative_when_alternative_unknown():
    result = assess(base_flight(cancelled=True, overrides={}))
    assert result["verdict"] == POSSIBLY
    assert any("accepted an alternative" in reason for reason in result["reasons"])


def test_foreign_carrier_arrival_to_saudi_does_not_claim_gaca_scope():
    result = assess(base_flight(
        airline_code="EK", origin="DXB", destination="JED",
        overrides={"actual_arrival": "2026-06-01 18:00"},
    ))
    assert not any("GACA" in framework for framework in result["frameworks"])
    assert result["verdict"] == NOT_ELIGIBLE


def test_inbound_eu_carrier_is_assessed_under_eu261():
    result = assess(base_flight(
        airline_code="AF", origin="DXB", destination="CDG",
        overrides={"actual_arrival": "2026-06-01 15:00"},
    ))
    assert result["verdict"] == ELIGIBLE
    assert "EU Regulation 261/2004" in result["frameworks"]


def test_incomplete_itinerary_requests_details_instead_of_overclaiming():
    result = assess({"airline_code": "SV", "overrides": {}})
    assert result["verdict"] == UNKNOWN
    assert any("route is incomplete" in reason.lower() for reason in result["reasons"])
