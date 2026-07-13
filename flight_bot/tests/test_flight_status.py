from flight_bot.flight_status import flightaware_status, live_landed


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
