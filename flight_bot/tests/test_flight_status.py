from datetime import datetime, timezone

from flight_bot.flight_status import (airplanes_live_status, flightaware_status,
                                      fuse_status, live_landed,
                                      poll_interval_seconds)


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"flights": [{
            "origin": {"code_iata": "RUH"},
            "destination": {"code_iata": "JED"},
            "scheduled_out": "2026-07-14T10:00:00Z",
            "actual_on": "2026-07-14T11:20:00Z",
            "status": "Arrived",
        }]}


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def flight():
    return {
        "flight_number": "SV 100", "flight_date": "2026-07-14",
        "origin": "RUH", "destination": "JED", "overrides": {},
    }


def test_flightaware_lookup_uses_official_api_and_detects_landed():
    session = FakeSession()
    record = flightaware_status(flight(), "secret", session=session)
    assert record["status"] == "Arrived"
    url, request = session.calls[0]
    assert url == "https://aeroapi.flightaware.com/aeroapi/flights/SV100"
    assert request["headers"] == {"x-apikey": "secret"}
    config = {
        "flight_status": {"provider": "flightaware",
                          "flightaware_api_key": "secret"}}
    assert live_landed(config, flight(), session=session) is True


def test_flightaware_rejects_wrong_route_instead_of_returning_weak_best_guess():
    class WrongRouteResponse(FakeResponse):
        def json(self):
            return {"flights": [{
                "origin": {"code_iata": "DMM"},
                "destination": {"code_iata": "MED"},
                "scheduled_out": "2026-07-14T10:00:00Z",
                "status": "Arrived",
            }]}

    class WrongRouteSession(FakeSession):
        def get(self, url, **kwargs):
            return WrongRouteResponse()

    assert flightaware_status(flight(), "secret", session=WrongRouteSession()) is None


def test_account_free_adsb_requires_exact_callsign():
    class Response(FakeResponse):
        def json(self):
            return {"ac": [
                {"flight": "SV 101", "seen": 0.1},
                {"flight": "SV100 ", "seen": 1.2, "alt_baro": 32000},
            ]}

    class Session(FakeSession):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    result = airplanes_live_status(flight(), session=session)
    assert result["flight"].strip() == "SV100"
    assert session.calls[0][0].endswith("/callsign/SV100")


def test_adsb_converts_saudia_iata_number_to_operational_callsign():
    class Response(FakeResponse):
        def json(self):
            return {"ac": [{"flight": "SVA1671", "seen": 0.2,
                            "alt_baro": 28000}]}

    class Session(FakeSession):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    item = {**flight(), "airline_code": "SV", "flight_number": "SV1671"}
    session = Session()
    assert airplanes_live_status(item, session=session)["flight"] == "SVA1671"
    assert session.calls[0][0].endswith("/callsign/SVA1671")


def test_fusion_surfaces_cancellation_airborne_conflict():
    now = datetime(2026, 7, 14, 10, tzinfo=timezone.utc)
    base = {
        "flight_key": "sv100", "observed_at": now.isoformat(),
        "source_timestamp": now.isoformat(), "data": {},
    }
    snapshot = fuse_status(flight(), [
        {**base, "provider": "booking_email", "status": "cancelled",
         "confidence": .98},
        {**base, "provider": "airplanes_live", "status": "airborne",
         "confidence": .82},
    ], now=now)
    assert snapshot["status"] == "cancelled"
    assert snapshot["contradictions"]


def test_adaptive_polling_is_fast_only_near_or_during_flight():
    item = {**flight(), "departure": "2026-07-14 10:00",
            "arrival": "2026-07-14 12:00"}
    assert poll_interval_seconds(
        item, now=datetime(2026, 7, 14, 9, 0)) == 10 * 60
    assert poll_interval_seconds(
        item, now=datetime(2026, 7, 14, 11, 0)) == 4 * 60
    assert poll_interval_seconds(
        item, now=datetime(2026, 7, 12, 10, 0)) == 6 * 60 * 60
