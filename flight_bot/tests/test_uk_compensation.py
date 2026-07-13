from flight_bot.compensation import ELIGIBLE, assess


def test_eu_carrier_arriving_in_uk_is_covered_by_uk261():
    result = assess({
        "airline_code": "AF",
        "origin": "JFK",
        "destination": "LHR",
        "flight_date": "2026-06-01",
        "arrival": "2026-06-01 10:00",
        "overrides": {"actual_arrival": "2026-06-01 13:30"},
    })
    assert result["verdict"] == ELIGIBLE
    assert "UK261 passenger rights" in result["frameworks"]
    assert any("GBP 220–520" in remedy for remedy in result["remedies"])
