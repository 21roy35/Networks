from flight_bot import db, gaca_account, gaca_normal_browser
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


def test_read_only_navigation_continues_after_slow_dom_content():
    class Page:
        def __init__(self):
            self.goto_args = None
            self.waited = []

        def goto(self, *args, **kwargs):
            self.goto_args = (args, kwargs)

        def wait_for_load_state(self, *_args, **_kwargs):
            raise RuntimeError("third-party resource still loading")

        def wait_for_timeout(self, milliseconds):
            self.waited.append(milliseconds)

    page = Page()
    gaca_account._navigate_read_only(page, "https://example.test", 750)

    assert page.goto_args[1] == {
        "wait_until": "commit",
        "timeout": 30000,
    }
    assert page.waited == [750]


def test_normal_browser_recycles_one_unresponsive_cdp_session(
        tmp_path, monkeypatch):
    context = object()

    class Browser:
        contexts = [context]

    class Chromium:
        def __init__(self):
            self.calls = 0

        def connect_over_cdp(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("stale CDP")
            return Browser()

    chromium = Chromium()
    playwright = type("Playwright", (), {"chromium": chromium})()
    recycled = []
    launched = []
    monkeypatch.setattr(gaca_normal_browser, "_cdp_port", lambda: 9225)
    monkeypatch.setattr(
        gaca_normal_browser, "_profile_dir", lambda _default: tmp_path)
    monkeypatch.setattr(gaca_normal_browser, "_cdp_ready", lambda _port: True)
    monkeypatch.setattr(
        gaca_normal_browser, "shutdown",
        lambda *_args, **_kwargs: recycled.append(True))
    monkeypatch.setattr(
        gaca_normal_browser, "_start_normal_chrome",
        lambda port, profile: launched.append((port, profile)))
    monkeypatch.setattr(
        gaca_normal_browser, "_sync_proxy_session_state",
        lambda *_args, **_kwargs: False)

    browser, returned_context = gaca_normal_browser.connect(
        playwright, profile_dir=tmp_path)

    assert isinstance(browser, Browser)
    assert returned_context is context
    assert chromium.calls == 2
    assert recycled == [True]
    assert launched == [(9225, tmp_path)]


def test_normal_browser_shutdown_preserves_profile_and_stops_process(
        monkeypatch):
    class Browser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 5

    browser = Browser()
    process = Process()
    monkeypatch.setattr(
        gaca_normal_browser, "_NORMAL_CHROME_PROCESS", process)

    gaca_normal_browser.shutdown(browser)

    assert browser.closed is True
    assert process.terminated is True
    assert gaca_normal_browser._NORMAL_CHROME_PROCESS is None
