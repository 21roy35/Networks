import os

import pytest

from flight_bot import db, gaca_normal_browser, portal_automation
from flight_bot.airlines import AIRLINES
from flight_bot.complaints import complaint_payload, missing_portal_fields
from flight_bot.portal_automation import (_extract_reference,
                                          _extract_reference_from_url,
                                          _is_official_url,
                                          _playwright_proxy_options)


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


def test_proxy_credentials_are_passed_separately_to_playwright():
    assert _playwright_proxy_options(
        "http://sticky%2Bsession:p%40ss@proxy.example:9000") == {
            "server": "http://proxy.example:9000",
            "username": "sticky+session",
            "password": "p@ss",
        }


def test_proxy_without_credentials_remains_usable():
    assert _playwright_proxy_options("proxy.example:9000") == {
        "server": "http://proxy.example:9000",
    }


def test_portal_job_payload_and_retry_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightdeck.db")
    db.init_db()
    job = {
        "id": "durable-1",
        "kind": "airline",
        "airline_code": "SV",
        "flight_number": "SV100",
        "flight_key": "flight-1",
        "status": "queued",
        "message": "queued",
        "payload": {"kind": "airline", "incident": "broken seat"},
    }
    db.save_portal_job(job)
    claimed = db.claim_portal_job(job["id"])
    assert claimed["attempts"] == 1
    assert claimed["payload"]["incident"] == "broken seat"

    next_attempt = db.retry_portal_job(
        job["id"], "blocked before Submit", minimum_delay=60)
    restored = db.get_portal_job(job["id"])
    assert next_attempt is not None
    assert restored["status"] == "retry_wait"
    assert restored["payload"] == job["payload"]
    assert restored["next_attempt_at"] == next_attempt


def test_complaint_lineage_keeps_immutable_original_and_child_context(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lineage.db")
    db.init_db()
    root_id = db.begin_complaint(
        "flight-1", "airline", "Original",
        "My seat would not recline.",
        submitted_text="Original expanded portal text",
        issue_summary="The seat would not recline.",
        requested_resolution_summary="Repair and a fair resolution.",
    )
    db.finish_complaint(root_id, "submitted", "C_100")
    db.close_complaint(root_id)
    child_id = db.begin_complaint(
        "flight-1", "airline", "Follow-up",
        "I raised this under C_100 and it was closed without a solution.",
        parent_complaint_id=root_id,
        escalate_parent_on_success=True,
    )
    child = db.get_complaint(child_id)
    root = db.get_complaint(root_id)

    assert root["root_complaint_id"] == root_id
    assert child["parent_complaint_id"] == root_id
    assert child["root_complaint_id"] == root_id
    assert child["generation"] == 1
    assert child["original_text"] == "My seat would not recline."
    assert child["details"].startswith("I raised this under C_100")


def test_safe_portal_jobs_use_durable_long_lived_retry_budget(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "retries.db")
    db.init_db()
    db.save_portal_job({
        "id": "durable-safe",
        "status": "queued",
        "payload": {"kind": "gaca"},
        "max_attempts": 100000,
    })
    claimed = db.claim_portal_job("durable-safe")
    assert claimed["max_attempts"] == 100000
    assert db.retry_portal_job(
        "durable-safe", "confirmed pre-submit failure",
        minimum_delay=60) is not None
    assert db.get_portal_job("durable-safe")["status"] == "retry_wait"


def test_remediated_proxy_retry_bypasses_old_exponential_backoff(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "proxy-retry.db")
    db.init_db()
    db.save_portal_job({
        "id": "proxy-remediated",
        "status": "queued",
        "payload": {"kind": "gaca"},
        "max_attempts": 100000,
    })
    with db.connect() as conn:
        conn.execute(
            "UPDATE portal_jobs SET attempts=8 WHERE id=?",
            ("proxy-remediated",),
        )
    before = db.time.time()
    due = db.retry_portal_job(
        "proxy-remediated",
        "GACA residential proxy was rotated before Submit.",
        fixed_delay=90,
    )

    assert due is not None
    assert 85 <= due - before <= 95


def test_gaca_rate_limit_defers_and_serializes_all_gaca_jobs(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gaca-circuit.db")
    db.init_db()
    for job_id, kind in (
            ("gaca-current", "gaca"),
            ("gaca-sibling", "gaca"),
            ("airline-job", "airline")):
        db.save_portal_job({
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "payload": {"kind": kind},
            "max_attempts": 100000,
        })
    assert db.claim_portal_job("gaca-current") is not None
    before = db.time.time()

    first_due = db.defer_gaca_portal_jobs(
        "gaca-current",
        "GACA reported too many submission attempts.",
        delay=24 * 3600,
        spacing=6 * 3600,
    )

    current = db.get_portal_job("gaca-current")
    sibling = db.get_portal_job("gaca-sibling")
    airline = db.get_portal_job("airline-job")
    assert 24 * 3600 - 5 <= first_due - before <= 24 * 3600 + 5
    assert current["status"] == "retry_wait"
    assert current["next_attempt_at"] == first_due
    assert sibling["status"] == "retry_wait"
    assert sibling["next_attempt_at"] == first_due + 6 * 3600
    assert airline["status"] == "queued"
    assert db.gaca_portal_circuit_until() == first_due


def test_new_gaca_job_cannot_bypass_shared_rate_limit_circuit(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gaca-circuit-new.db")
    db.init_db()
    db.save_portal_job({
        "id": "gaca-original",
        "kind": "gaca",
        "status": "queued",
        "payload": {"kind": "gaca"},
    })
    db.defer_gaca_portal_jobs(
        "gaca-original", "rate limited", delay=3600, spacing=900)
    db.save_portal_job({
        "id": "gaca-new",
        "kind": "gaca",
        "status": "queued",
        "payload": {"kind": "gaca"},
    })
    db.save_portal_job({
        "id": "airline-due",
        "kind": "airline",
        "status": "queued",
        "payload": {"kind": "airline"},
    })

    assert [item["id"] for item in db.list_due_portal_jobs(10)] == [
        "airline-due",
    ]


def test_legacy_job_restores_routing_fields_before_worker(monkeypatch):
    captured = {}

    def submit(payload, _update):
        captured.update(payload)
        return portal_automation.PortalResult(
            "error", "safe test stop", retry_safe=True)

    monkeypatch.setattr(portal_automation, "submit_portal_claim", submit)
    monkeypatch.setattr(
        portal_automation.db, "retry_portal_job",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        portal_automation.db, "quarantine_portal_job",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        portal_automation.db, "save_portal_job",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        portal_automation, "_finalize_persisted_complaint",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        portal_automation.threading.Thread, "start",
        lambda thread: thread.run())

    portal_automation._start_portal_worker({
        "id": "legacy-empty-payload",
        "kind": "gaca",
        "airline_code": "SV",
        "flight_number": "SV1674",
        "flight_key": "PNR|SV1674|2026-07-09",
        "complaint_id": 96,
        "status": "leased",
        "payload": {},
    })

    assert captured["kind"] == "gaca"
    assert captured["airline_code"] == "SV"
    assert captured["flight_number"] == "SV1674"
    assert captured["flight_key"] == "PNR|SV1674|2026-07-09"
    assert captured["portal_complaint_id"] == 96


def test_interrupted_submit_is_quarantined_but_opening_is_requeued(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightdeck.db")
    db.init_db()
    for job_id, status in (("safe", "opening"), ("ambiguous", "submitting")):
        db.save_portal_job({
            "id": job_id,
            "status": status,
            "message": status,
            "payload": {"kind": "airline"},
        })

    recovered = db.recover_interrupted_portal_jobs()

    assert recovered == {"retry_wait": 1, "quarantined": 1}
    assert db.get_portal_job("safe")["status"] == "retry_wait"
    assert db.get_portal_job("ambiguous")["status"] == "quarantined"


def test_interrupted_gaca_submit_reconciles_then_requeues(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightdeck.db")
    db.init_db()
    db.save_portal_job({
        "id": "gaca-submit",
        "kind": "gaca",
        "status": "submitting",
        "message": "Submit clicked",
        "payload": {"kind": "gaca"},
    })

    recovered = db.recover_interrupted_portal_jobs()
    job = db.get_portal_job("gaca-submit")

    assert recovered == {"retry_wait": 1, "quarantined": 0}
    assert job["status"] == "retry_wait"
    assert job["terminal"] == 0
    assert job["next_attempt_at"] is not None
    assert "not verified" in job["message"]


def test_legacy_ambiguous_gaca_jobs_become_one_canonical_retry(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightdeck.db")
    db.init_db()
    db.add_complaint(
        "PNR|SV1|2026-07-01", "gaca", None, "Older", "failed")
    db.add_complaint(
        "PNR|SV1|2026-07-01", "gaca", None, "Newer", "failed")
    complaints = db.complaints_for_flight("PNR|SV1|2026-07-01")
    older, newer = (int(complaints[-2]["id"]), int(complaints[-1]["id"]))
    db.save_portal_job({
        "id": "older-unknown",
        "kind": "gaca",
        "flight_key": "PNR|SV1|2026-07-01",
        "complaint_id": older,
        "status": "confirmation_unknown",
        "message": "Unreadable confirmation",
        "terminal": True,
    })
    db.save_portal_job({
        "id": "newer-quarantine",
        "kind": "gaca",
        "flight_key": "PNR|SV1|2026-07-01",
        "complaint_id": newer,
        "status": "quarantined",
        "message": "Unreadable confirmation",
        "terminal": True,
    })
    with db.connect() as conn:
        conn.execute(
            "UPDATE portal_jobs SET created_at='2026-07-01 00:00:00' "
            "WHERE id='older-unknown'")
        conn.execute(
            "UPDATE portal_jobs SET created_at='2026-07-02 00:00:00' "
            "WHERE id='newer-quarantine'")

    db.init_db()

    assert db.get_portal_job("newer-quarantine")["status"] == "retry_wait"
    assert db.get_portal_job("older-unknown")["status"] == "superseded"
    assert db.get_complaint(newer)["status"] == "filing"
    assert db.get_complaint(older)["status"] == "failed"


def test_retry_safety_requires_pre_submit_or_explicit_rejection():
    generic = portal_automation.PortalResult(
        "error", "network stopped")
    rejected = portal_automation.PortalResult(
        "error", "portal rejected the request", retry_safe=True)
    assert portal_automation._retry_is_safe(generic, "opening") is True
    assert portal_automation._retry_is_safe(generic, "submitting") is False
    assert portal_automation._retry_is_safe(rejected, "submitting") is True


def test_ghala_can_verify_gaca_failure_but_not_invent_acceptance(monkeypatch):
    class Page:
        url = "https://myeservices.gaca.gov.sa/error"

        def is_closed(self):
            return False

    monkeypatch.setattr(
        portal_automation, "_ask_ai",
        lambda *_args, **_kwargs: {
            "state": "error", "summary": "The page is a WAF error.",
            "confidence": 0.98,
        })
    monkeypatch.setattr(
        portal_automation, "_body_text",
        lambda _page: "This page can't be displayed. Incident ID: 123")
    monkeypatch.setattr(portal_automation, "_AI_HANDLER", object())

    failed = portal_automation._gaca_ai_submission_result(
        Page(), {"kind": "gaca"}, "WAF after Submit")
    assert failed.status == "error"
    assert failed.retry_safe is True

    monkeypatch.setattr(
        portal_automation, "_ask_ai",
        lambda *_args, **_kwargs: {
            "state": "submitted", "summary": "It may be submitted.",
            "confidence": 0.99,
        })
    assert portal_automation._gaca_ai_submission_result(
        Page(), {"kind": "gaca"}, "blank page") is None


def test_gaca_nafath_outage_uses_long_retry_cooldown():
    outage = portal_automation.PortalResult(
        "error",
        "GACA's Nafath authentication endpoint rejected the login "
        "(Authentication using Nafath failed).",
        retry_safe=True,
    )
    generic = portal_automation.PortalResult(
        "error", "The official site timed out.", retry_safe=True)
    rate_limited = portal_automation.PortalResult(
        "error",
        "GACA reported too many submission attempts and requested a "
        "waiting period before retry.",
        retry_safe=True,
    )

    assert portal_automation._retry_minimum_delay(outage) == 3600
    assert portal_automation._retry_minimum_delay(rate_limited) == 3600
    assert portal_automation._retry_minimum_delay(generic) == 300


def test_gaca_rate_limit_is_identified_for_fixed_retry_delay():
    message = (
        "GACA reported too many submission attempts and requested a "
        "waiting period before retry."
    )
    assert portal_automation._retry_minimum_delay(
        portal_automation.PortalResult(
            "error", message, retry_safe=True)) == 3600


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


def test_payload_prefers_exact_saved_profile_names_and_alfursan_id():
    saved = profile()
    saved.update({
        "first_name": "ProfileFirst",
        "middle_name": "ProfileMiddle",
        "last_name": "ProfileLast",
        "alfursan_id": "30680000",
    })
    payload = complaint_payload(
        sample_flight(), saved, "airline",
        "The seat and entertainment screen were both broken.")
    assert payload["passenger_name"] == (
        "ProfileFirst ProfileMiddle ProfileLast")
    assert payload["first_name"] == "ProfileFirst"
    assert payload["middle_name"] == "ProfileMiddle"
    assert payload["last_name"] == "ProfileLast"
    assert payload["alfursan_id"] == "30680000"


def test_family_booking_never_inherits_primary_users_identity():
    flight = sample_flight()
    flight["passenger"] = "Muhannad Alqahtani"
    owner = profile()
    owner.update({
        "full_name": "Mansour Albu Asais",
        "first_name": "Mansour",
        "middle_name": "Albu",
        "last_name": "Asais",
        "national_id": "OWNER-ID",
        "alfursan_id": "30689772",
    })

    payload = complaint_payload(
        flight, owner, "airline",
        "The flight was cancelled without a suitable alternative.")

    assert payload["passenger_name"] == "Muhannad Alqahtani"
    assert payload["first_name"] == "Muhannad"
    assert payload["last_name"] == "Alqahtani"
    assert payload["national_id"] == ""
    assert payload["title"] == ""
    assert payload["nationality"] == ""
    assert payload["alfursan_id"] == ""
    assert payload["email"] == "passenger@example.com"
    assert missing_portal_fields(payload) == [
        "saved identity profile for Muhannad Alqahtani"]


def test_family_booking_uses_only_that_passengers_saved_profile():
    flight = sample_flight()
    flight["passenger"] = "Muhannad Alqahtani"
    owner = profile()
    owner.update({
        "full_name": "Mansour Albu Asais",
        "first_name": "Mansour",
        "national_id": "OWNER-ID",
        "alfursan_id": "30689772",
    })
    family = {
        "muhannad alqahtani": {
            "booking_name": "Muhannad Alqahtani",
            "full_name": "Muhannad Alqahtani",
            "first_name": "Muhannad",
            "last_name": "Alqahtani",
            "email": "muhannad@example.com",
            "phone": "+966511111111",
            "country_code": "+966",
            "national_id": "MUHANNAD-ID",
            "title": "Mr",
            "nationality": "Saudi Arabian",
            "alfursan_id": "FAMILY-123",
        },
    }

    payload = complaint_payload(
        flight, owner, "airline",
        "The flight was cancelled without a suitable alternative.",
        passenger_profiles=family)

    assert payload["passenger_name"] == "Muhannad Alqahtani"
    assert payload["national_id"] == "MUHANNAD-ID"
    assert payload["alfursan_id"] == "FAMILY-123"
    assert payload["email"] == "muhannad@example.com"
    assert payload["passenger_profile_missing"] is False
    assert missing_portal_fields(payload) == []


def test_ai_analysis_writes_a_natural_first_person_complaint():
    original = "My seat was broken and the screen did not work."
    payload = complaint_payload(
        sample_flight(), profile(), "airline", original,
        ai_analysis={
            "category": "seat",
            "summary": "I could not use my seat or entertainment screen.",
            "facts": ["The seat was broken", "The screen did not work"],
            "evidence_observations": ["A seat component appears displaced"],
            "requested_remedy": "Please investigate this and provide a fair resolution.",
        })
    assert payload["incident"] == original
    assert payload["ai_analysis"]["category"] == "seat"
    assert payload["description"].startswith("Hello,")
    assert "I could not use my seat or entertainment screen." in payload["description"]
    assert "Please investigate this and provide a fair resolution." in payload["description"]
    assert "Passenger reports" not in payload["description"]
    assert "Additional issue summary" not in payload["description"]
    assert "Facts stated by the passenger" not in payload["description"]
    assert "financial compensation where applicable" not in payload["description"]


def test_gaca_free_text_does_not_repeat_structured_form_fields():
    flight = sample_flight()
    payload = complaint_payload(
        flight,
        profile(),
        "gaca",
        "The airline closed my complaint without fixing the broken screen.",
        airline_reference="C_2800078",
        airline_complaint_date="2026-07-15",
        ai_analysis={
            "summary": (
                "During my flight SV100 from RUH to JED, the airline closed "
                "my complaint C_2800078 without addressing the broken "
                "entertainment screen or offering a proper remedy."
            ),
            "requested_remedy": (
                "I want the issue reviewed and a fair cash remedy."
            ),
        },
    )

    description = payload["description"]
    for structured_value in (
        payload["national_id"],
        payload["passenger_name"],
        payload["flight_number"],
        payload["flight_date"],
        payload["pnr"],
        payload["ticket_number"],
        payload["airline_reference"],
        payload["airline_complaint_date"],
    ):
        assert structured_value not in description
    assert "broken entertainment screen" in description
    assert "fair cash remedy" in description


def test_old_third_person_analysis_is_safely_naturalized():
    flight = sample_flight()
    flight.update({
        "flight_number": "SV520", "flight_date": "2025-12-06",
        "origin": "RUH", "destination": "BAH", "pnr": "7MZ63V",
        "ticket_numbers": ["065-2191703682"],
    })
    payload = complaint_payload(
        flight, profile(), "airline", "no amentity kit was provided",
        ai_analysis={
            "category": "service",
            "summary": "Passenger reports that no amenity kit was provided.",
            "facts": ["No amenity kit was provided"],
            "evidence_observations": [],
            "requested_remedy": (
                "Passenger requests investigation and an appropriate remedy."),
        })
    description = payload["description"]
    assert "I am writing about my Saudia flight SV520 from RUH to BAH" in description
    assert "The issue I experienced was that no amenity kit was provided." in description
    assert "I would appreciate it if you could investigate this and provide an appropriate resolution." in description
    assert "Passenger reports" not in description
    assert "Assessment basis" not in description


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


def test_saudia_uses_current_public_complaint_form():
    assert AIRLINES["SV"]["complaint_url"] == (
        "https://www.saudia.com/en/forms/complaint-form")
    assert _is_official_url(AIRLINES["SV"]["complaint_url"])
    assert not _is_official_url(
        "https://booking-uat.dcloud.saudia.com/forms/contact-form")


def test_block_page_is_not_mistaken_for_a_form():
    class Body:
        def inner_text(self, timeout=None):
            return "The request is blocked. Tracking reference 123."

    class Page:
        def title(self):
            return "Service unavailable"

        def locator(self, selector):
            assert selector == "body"
            return Body()

    assert portal_automation._request_blocked(Page()) is True


def test_gaca_waf_error_page_is_detected_as_blocked():
    class Body:
        def inner_text(self, timeout=None):
            return ("Error This page can't be displayed. Contact support "
                    "for additional information. The incident ID is: N/A.")

    class Page:
        def title(self):
            return "Error"

        def locator(self, selector):
            assert selector == "body"
            return Body()

    assert portal_automation._request_blocked(Page()) is True


def test_gaca_generic_http_403_error_page_is_detected_as_blocked():
    class Body:
        def inner_text(self, timeout=None):
            return "Sorry! There is an error loading this page. Go Back"

    class Page:
        def title(self):
            return "GACA"

        def locator(self, selector):
            assert selector == "body"
            return Body()

    assert portal_automation._request_blocked(Page()) is True


def test_fill_updates_every_visible_duplicate_control():
    filled = []

    class Control:
        def __init__(self, name, visible):
            self.name = name
            self.visible = visible

        def is_visible(self):
            return self.visible

        def is_editable(self):
            return True

        def fill(self, value):
            filled.append((self.name, value))

    class Matches:
        def __init__(self, controls):
            self.controls = controls

        def count(self):
            return len(self.controls)

        def nth(self, index):
            return self.controls[index]

    class Page:
        def get_by_label(self, _pattern):
            return Matches([
                Control("earlier-step", True),
                Control("current-step", True),
            ])

        def get_by_placeholder(self, _pattern):
            return Matches([])

        def locator(self, _selector):
            raise AssertionError("fallback should not be needed")

    assert portal_automation._fill(Page(), ["last name"], "ProfileLast")
    assert filled == [
        ("current-step", "ProfileLast"),
        ("earlier-step", "ProfileLast"),
    ]


def test_fill_updates_controls_reached_by_different_label_aliases():
    filled = []

    class Control:
        def __init__(self, name):
            self.name = name

        def is_visible(self):
            return True

        def is_editable(self):
            return True

        def fill(self, value):
            filled.append((self.name, value))

    class Matches:
        def __init__(self, controls=()):
            self.controls = list(controls)

        def count(self):
            return len(self.controls)

        def nth(self, index):
            return self.controls[index]

    class Page:
        def get_by_label(self, pattern):
            if pattern.pattern == "email address":
                return Matches([Control("email-address")])
            if pattern.pattern == "email":
                return Matches([Control("email-required")])
            return Matches()

        def get_by_placeholder(self, _pattern):
            return Matches()

        def locator(self, _selector):
            raise AssertionError("fallback should not be needed")

    assert portal_automation._fill(
        Page(), ["email address", "e-mail address", "email"],
        "saved@example.com")
    assert filled == [
        ("email-address", "saved@example.com"),
        ("email-required", "saved@example.com"),
    ]


def test_material_dropdown_is_selected_from_allowed_choice():
    selected = []

    class Empty:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def all(self):
            return []

        def is_visible(self):
            return False

    class Control:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, **_kwargs):
            pass

    class Option:
        def __init__(self, text):
            self.text = text

        def is_visible(self):
            return True

        def inner_text(self):
            return self.text

        def click(self, **_kwargs):
            selected.append(self.text)

    class Options:
        def __init__(self):
            self.values = [Option("Please Select"),
                           Option("Travel complaint or compliment")]

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Field:
        def is_visible(self):
            return True

        def inner_text(self):
            return "Please Select Service type *"

        def locator(self, selector):
            if selector == "mat-select":
                return Control()
            if selector == "input:not([type=hidden])":
                return Empty()
            if selector == "mat-label":
                class Label(Control):
                    def inner_text(self):
                        return "Service type *"
                return Label()
            raise AssertionError(selector)

    class Fields:
        def all(self):
            return [Field()]

    class Keyboard:
        def press(self, _key):
            pass

    class Page:
        keyboard = Keyboard()

        def get_by_label(self, _pattern):
            return Empty()

        def locator(self, selector):
            if selector == "select":
                return Empty()
            if selector == "mat-form-field":
                return Fields()
            if selector == "mat-option, [role='option']":
                return Options()
            raise AssertionError(selector)

        def wait_for_timeout(self, _milliseconds):
            pass

    assert portal_automation._select(
        Page(), ["service type"], ["travel complaint or compliment"])
    assert selected == ["Travel complaint or compliment"]


def test_material_autocomplete_is_searched_and_selected():
    selected = []
    searches = []

    class Empty:
        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def all(self):
            return []

        def is_visible(self):
            return False

    class Input:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def is_editable(self):
            return True

        def click(self, **_kwargs):
            pass

        def fill(self, value):
            searches.append(value)

    class Option:
        def is_visible(self):
            return True

        def inner_text(self):
            return "Saudi Arabia (+966)"

        def click(self, **_kwargs):
            selected.append("Saudi Arabia (+966)")

    class Options:
        def count(self):
            return 1

        def nth(self, _index):
            return Option()

    class Label(Input):
        def inner_text(self):
            return "Country or territory code*"

    class Field:
        def is_visible(self):
            return True

        def inner_text(self):
            return "Country or territory code*"

        def locator(self, selector):
            return {
                "mat-label": Label(),
                "mat-select": Empty(),
                "input:not([type=hidden])": Input(),
            }[selector]

    class Fields:
        def all(self):
            return [Field()]

    class Keyboard:
        def press(self, _key):
            pass

    class Page:
        keyboard = Keyboard()

        def get_by_label(self, _pattern):
            return Empty()

        def locator(self, selector):
            return {
                "select": Empty(),
                "mat-form-field": Fields(),
                "mat-option, [role='option']": Options(),
            }[selector]

        def wait_for_timeout(self, _milliseconds):
            pass

    assert portal_automation._select(
        Page(), ["country or territory code"],
        [r"\+966", "Saudi Arabia"], queries=["966"])
    assert selected == ["Saudi Arabia (+966)"]


def test_saudia_feedback_survey_is_dismissed_before_form_fill():
    clicked = []

    class Locator:
        def __init__(self, visible=True):
            self.visible = visible
            self.first = self

        def count(self):
            return int(self.visible)

        def is_visible(self):
            return self.visible

        def click(self, **_kwargs):
            clicked.append("close")

    class Frame:
        url = "https://resources.example.medallia.com/md-form/index.html"

        def get_by_role(self, _role, name=None):
            return Locator()

        def locator(self, _selector):
            return Locator(False)

    class Page:
        frames = [Frame()]

        def wait_for_timeout(self, _milliseconds):
            pass

    assert portal_automation._dismiss_feedback_overlay(Page()) is True
    assert clicked == ["close"]


def test_recaptcha_uses_interactive_anchor_when_placeholder_comes_first():
    class Empty:
        first = None

        def count(self):
            return 0

    class Checkbox:
        def __init__(self):
            self.first = self
            self.checked = False

        def count(self):
            return 1

        def is_visible(self):
            return True

        def get_attribute(self, name):
            assert name == "aria-checked"
            return "true" if self.checked else "false"

        def click(self):
            self.checked = True

    class Frame:
        def __init__(self, checkbox=None):
            self.url = "https://recaptcha.net/recaptcha/api2/anchor"
            self.checkbox = checkbox

        def locator(self, selector):
            assert selector == "#recaptcha-anchor"
            return self.checkbox or Empty()

    class Page:
        def __init__(self, frames):
            self.frames = frames

        def wait_for_timeout(self, _milliseconds):
            pass

    checkbox = Checkbox()
    page = Page([Frame(), Frame(checkbox)])
    assert portal_automation._solve_recaptcha(page, lambda *_: None) is True
    assert checkbox.checked is True


def test_2captcha_extracts_site_key_and_applies_token(monkeypatch):
    class Frame:
        url = ("https://recaptcha.net/recaptcha/api2/anchor?"
               "k=saudia-site-key&size=normal")

    class Page:
        url = "https://booking.saudia.com/forms/contact-form"
        frames = [Frame()]

        def __init__(self):
            self.waits = []
            self.injected = ""
            self.inject_script = ""

        def evaluate(self, script, *args):
            if script == "navigator.userAgent":
                return "Modern Browser"
            self.inject_script = script
            self.injected = args[0]
            return {"fields": 1, "callbacks": 1}

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    captured = {}

    def solve(challenge):
        captured.update(challenge)
        return {"token": "automatic-token", "task_id": "123"}

    monkeypatch.setattr(portal_automation, "_CAPTCHA_SOLVER", solve)
    page = Page()
    updates = []
    assert portal_automation._solve_recaptcha_automatically(
        page, lambda *args: updates.append(args)) is True
    assert captured == {
        "kind": "recaptcha",
        "website_url": page.url,
        "site_key": "saudia-site-key",
        "is_invisible": False,
        "is_enterprise": False,
        "user_agent": "Modern Browser",
        "api_domain": "recaptcha.net",
    }
    assert page.injected == "automatic-token"
    assert "document.createElement" in page.inject_script
    assert "g-recaptcha-response" in page.inject_script
    assert page.waits == [1200]
    assert updates[0][0] == "verification"
    assert updates[-1][0] == "filling"


def test_gaca_native_post_requires_the_injected_token():
    class Form:
        def __init__(self, result):
            self.result = result
            self.script = ""

        def evaluate(self, script):
            self.script = script
            return self.result

    accepted = Form(True)
    assert portal_automation._submit_gaca_form_with_injected_token(
        accepted) is True
    assert "checkValidity" in accepted.script
    assert "HTMLFormElement.prototype.submit.call" in accepted.script

    rejected = Form(False)
    assert portal_automation._submit_gaca_form_with_injected_token(
        rejected) is False


def test_recaptcha_detects_execute_based_v3_and_action():
    class Frame:
        url = ("https://www.google.com/recaptcha/api2/anchor?"
               "k=gaca-site-key&size=invisible")

    class Page:
        url = "https://myeservices.gaca.gov.sa/eservices/complaint/step4"
        frames = [Frame()]

        def evaluate(self, script, *args):
            if script == "navigator.userAgent":
                return "Modern Browser"
            assert "RECAPTCHA_PAGE_CONTEXT" in script
            assert args == ("gaca-site-key",)
            return {
                "is_v3": True,
                "is_enterprise": False,
                "page_action": "complaint_submit",
            }

    assert portal_automation._recaptcha_challenge(Page()) == {
        "kind": "recaptcha",
        "website_url": Page.url,
        "site_key": "gaca-site-key",
        "is_invisible": True,
        "is_enterprise": False,
        "user_agent": "Modern Browser",
        "api_domain": "google.com",
        "is_v3": True,
        "page_action": "complaint_submit",
        "min_score": 0.9,
    }


def test_recaptcha_v3_key_is_recovered_from_render_script_before_iframe():
    class Empty:
        first = None

        def get_attribute(self, _name):
            return None

    class Scripts:
        def evaluate_all(self, _script):
            return [
                "https://www.google.com/recaptcha/api.js?"
                "render=gaca-script-site-key",
            ]

    class Page:
        url = "https://myeservices.gaca.gov.sa/eservices/complaint/step4"
        frames = []

        def locator(self, selector):
            if selector == "script[src*='recaptcha'][src*='render=']":
                return Scripts()
            return Empty()

        def evaluate(self, script, *args):
            if script == "navigator.userAgent":
                return "Modern Browser"
            assert "RECAPTCHA_PAGE_CONTEXT" in script
            assert args == ("gaca-script-site-key",)
            return {
                "is_v3": True,
                "is_enterprise": False,
                "page_action": "complaint",
            }

    assert portal_automation._recaptcha_challenge(Page()) == {
        "kind": "recaptcha",
        "website_url": Page.url,
        "site_key": "gaca-script-site-key",
        "is_invisible": True,
        "is_enterprise": False,
        "user_agent": "Modern Browser",
        "api_domain": "google.com",
        "is_v3": True,
        "page_action": "complaint",
        "min_score": 0.9,
    }


def test_nested_hcaptcha_is_detected_and_sent_to_automatic_solver(monkeypatch):
    class Empty:
        first = None

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Checkbox:
        def count(self):
            return 1

        def get_attribute(self, name):
            assert name == "aria-checked"
            return "false"

    class Frame:
        url = ("https://newassets.hcaptcha.com/captcha/v1/widget.html#"
               "frame=checkbox&sitekey=saudia-hcaptcha-key&size=normal")

        def locator(self, selector):
            return Checkbox() if selector == "#checkbox" else Empty()

    class Page:
        url = "https://www.saudia.com/en/forms/complaint-form"

        def __init__(self):
            self.frames = [Frame()]
            self.waits = []

        def locator(self, _selector):
            return Empty()

        def evaluate(self, script):
            assert script == "navigator.userAgent"
            return "Modern Browser"

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    captured = {}

    def solve(challenge):
        captured.update(challenge)
        return {"token": "hcaptcha-token"}

    monkeypatch.setattr(portal_automation, "_CAPTCHA_SOLVER", solve)
    monkeypatch.setattr(portal_automation, "_inject_hcaptcha_token",
                        lambda _page, token: token == "hcaptcha-token")
    page = Page()
    assert portal_automation._needs_human_step(
        page) == "Solve the CAPTCHA challenge."
    assert portal_automation._solve_hcaptcha_automatically(
        page, lambda *_args: None) is True
    assert captured == {
        "kind": "hcaptcha",
        "website_url": page.url,
        "site_key": "saudia-hcaptcha-key",
        "is_invisible": False,
        "user_agent": "Modern Browser",
    }
    assert page.waits == [1500]


def test_2captcha_failure_falls_back_to_telegram_grid(monkeypatch):
    class Visible:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

    class Page:
        def locator(self, _selector):
            return Visible()

        def is_closed(self):
            return False

    messages = iter(["Solve the CAPTCHA challenge.", ""])
    manual = []
    monkeypatch.setattr(portal_automation, "_needs_human_step",
                        lambda _page: next(messages))
    monkeypatch.setattr(portal_automation, "_CAPTCHA_SOLVER",
                        lambda _challenge: None)
    monkeypatch.setattr(portal_automation, "_VERIFICATION_HANDLER",
                        lambda _challenge: "1")
    monkeypatch.setattr(portal_automation, "_solve_otp",
                        lambda *_args: False)
    monkeypatch.setattr(portal_automation, "_solve_text_captcha",
                        lambda *_args: False)
    monkeypatch.setattr(portal_automation, "_solve_recaptcha_automatically",
                        lambda *_args: False)
    monkeypatch.setattr(portal_automation, "_solve_recaptcha",
                        lambda *_args: manual.append(True) or True)
    updates = []
    assert portal_automation._wait_for_human_step(
        Page(), lambda *args: updates.append(args)) is True
    assert manual == [True]
    assert any("Falling back to Telegram" in message
               for _stage, message in updates)


def test_solved_recaptcha_widget_is_not_reported_as_pending_step():
    class Widget:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def input_value(self):
            return ""

        def nth(self, _index):
            return self

        def inner_text(self, **_kwargs):
            return ""

    class Checkbox:
        def __init__(self, checked):
            self.checked = checked

        def get_attribute(self, name):
            assert name == "aria-checked"
            return "true" if self.checked else "false"

    class AnchorFrame:
        url = "https://www.google.com/recaptcha/api2/anchor?k=x"

        def __init__(self, checked):
            self.checkbox = Checkbox(checked)

        def locator(self, selector):
            assert selector == "#recaptcha-anchor"
            return self.checkbox

    class Page:
        def __init__(self, checked):
            self.frames = [AnchorFrame(checked)]

        def locator(self, selector):
            if "captcha" in selector or selector == "body":
                return Widget()
            empty = Widget()
            empty.count = lambda: 0
            return empty

    assert portal_automation._needs_human_step(
        Page(checked=False)) == "Solve the CAPTCHA challenge."
    assert portal_automation._needs_human_step(Page(checked=True)) == ""


def test_recaptcha_waits_for_stable_unselected_grid_before_next_prompt():
    class Count:
        def __init__(self, value):
            self.value = value

        def count(self):
            return self.value

    class Images(Count):
        def evaluate_all(self, _script):
            return True

    class Grid(Count):
        def __init__(self):
            super().__init__(1)
            self.first = self

        def is_visible(self):
            return True

        def locator(self, selector):
            assert selector == "img"
            return Images(1)

        def screenshot(self, **_kwargs):
            return b"stable-grid"

    class ChallengeFrame:
        url = "https://recaptcha.net/recaptcha/api2/bframe"

        def __init__(self):
            self.grid = Grid()

        def locator(self, selector):
            if selector == "#rc-imageselect-target":
                return self.grid
            if selector == "#rc-imageselect-target td":
                return Count(9)
            if selector.endswith(".rc-imageselect-tileselected"):
                return Count(0)
            raise AssertionError(selector)

    class Checkbox:
        def get_attribute(self, name):
            assert name == "aria-checked"
            return "false"

    class Anchor:
        def locator(self, selector):
            assert selector == "#recaptcha-anchor"
            return Checkbox()

    class Page:
        frames = [ChallengeFrame()]

        def __init__(self):
            self.waits = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()
    verified, ready = portal_automation._wait_for_recaptcha_refresh(
        page, Anchor())
    assert (verified, ready) == (False, True)
    assert page.waits[0] == 4500
    assert 750 in page.waits


def test_recaptcha_can_complete_after_more_than_five_grids(monkeypatch):
    class Checkbox:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def get_attribute(self, _name):
            return "false"

        def click(self):
            pass

    class AnchorFrame:
        url = "https://recaptcha.net/recaptcha/api2/anchor"

        def __init__(self):
            self.checkbox = Checkbox()

        def locator(self, selector):
            assert selector == "#recaptcha-anchor"
            return self.checkbox

    class Cell:
        def click(self, **_kwargs):
            pass

    class Cells:
        def count(self):
            return 9

        def nth(self, _index):
            return Cell()

    class Grid:
        def screenshot(self, **_kwargs):
            return b"captcha-grid"

    class Button:
        def count(self):
            return 1

        def click(self, **_kwargs):
            pass

    class ChallengeFrame:
        url = "https://recaptcha.net/recaptcha/api2/bframe"

        def locator(self, selector):
            if selector == "#rc-imageselect-target td":
                return Cells()
            if selector == "#rc-imageselect-target":
                return Grid()
            if selector == "#recaptcha-verify-button":
                return Button()
            raise AssertionError(selector)

    class Page:
        def __init__(self):
            self.frames = [AnchorFrame(), ChallengeFrame()]

        def wait_for_timeout(self, _milliseconds):
            pass

    rounds = []

    def wait_for_refresh(_page, _anchor):
        rounds.append(len(rounds) + 1)
        return len(rounds) >= 6, True

    monkeypatch.setattr(portal_automation, "_wait_for_recaptcha_refresh",
                        wait_for_refresh)
    monkeypatch.setattr(portal_automation, "_ask_verification",
                        lambda *_args, **_kwargs: "1")
    monkeypatch.setattr(portal_automation, "_annotate_grid",
                        lambda image, _count: image)
    monkeypatch.setattr(portal_automation, "_body_text", lambda _frame: "cars")
    updates = []
    assert portal_automation._solve_recaptcha(
        Page(), lambda *args: updates.append(args)) is True
    assert rounds == [1, 2, 3, 4, 5, 6]
    assert updates[-1][0] == "filling"


def test_unstable_captcha_tile_reprompts_instead_of_crashing(monkeypatch):
    class Checkbox:
        first = property(lambda self: self)

        def count(self):
            return 1

        def is_visible(self):
            return True

        def get_attribute(self, _name):
            return "false"

        def click(self, **_kwargs):
            pass

    class AnchorFrame:
        url = "https://recaptcha.net/recaptcha/api2/anchor"

        def locator(self, _selector):
            return Checkbox()

    class StuckCell:
        def click(self, **_kwargs):
            raise TimeoutError("Locator.click: Timeout 30000ms exceeded.")

    class Cells:
        def count(self):
            return 16

        def nth(self, _index):
            return StuckCell()

    class Grid:
        def screenshot(self, **_kwargs):
            return b"captcha-grid"

    class ChallengeFrame:
        url = "https://recaptcha.net/recaptcha/api2/bframe"

        def locator(self, selector):
            if selector == "#rc-imageselect-target td":
                return Cells()
            if selector == "#rc-imageselect-target":
                return Grid()
            raise AssertionError(selector)

    class Page:
        def __init__(self):
            self.frames = [AnchorFrame(), ChallengeFrame()]

        def wait_for_timeout(self, _milliseconds):
            pass

    responses = iter(["13", ""])
    prompts = []

    def ask(_kind, message, _page, **_kwargs):
        prompts.append(message)
        return next(responses)

    monkeypatch.setattr(portal_automation, "_ask_verification", ask)
    monkeypatch.setattr(portal_automation, "_annotate_grid",
                        lambda image, _count: image)
    monkeypatch.setattr(portal_automation, "_body_text",
                        lambda _frame: "crosswalks")
    updates = []
    assert portal_automation._solve_recaptcha(
        Page(), lambda *args: updates.append(args)) is False
    assert len(prompts) == 2
    assert any("stopped responding" in text for _stage, text in updates)


@pytest.mark.parametrize(("incident", "category"), [
    ("The seat recline was broken.", "Seats"),
    ("The flight was delayed for five hours.", "Flight Delay"),
    ("My baggage was delayed and the suitcase was damaged.",
     "Quality of services"),
    ("The flight was cancelled.", "Flight Cancellation"),
    ("The cabin crew handled the issue badly.", "Flight attendants/Pilots"),
    ("The entertainment screen was broken.", "Quality of services"),
])
def test_saudia_complaint_category_mapping(incident, category):
    assert portal_automation._saudia_complaint_category({
        "incident": incident, "ai_analysis": {},
    }) == category


def test_gaca_screen_issue_uses_exact_three_level_category():
    assert portal_automation._gaca_categories({
        "incident": "The seat-back entertainment screen was broken."
    }) == ("On Board Services", "Entertainment Services",
           "In- flight Screens")


@pytest.mark.parametrize(("incident", "expected"), [
    (
        "The flight was cancelled without a suitable alternative.",
        ("Flights", "Flight Cancellation", "Flight Cancellation"),
    ),
    (
        "The flight was delayed for five hours.",
        ("Flights", "Flight Delay", "Flight Delay"),
    ),
])
def test_gaca_disruption_uses_exact_three_level_category(incident, expected):
    assert portal_automation._gaca_categories({
        "incident": incident,
    }) == expected


def test_gaca_proxy_rotation_clears_only_gaca_state_once(
        tmp_path, monkeypatch):
    class CDPSession:
        def __init__(self):
            self.calls = []
            self.detached = False

        def send(self, method, payload):
            self.calls.append((method, payload))

        def detach(self):
            self.detached = True

    class Context:
        class Page:
            url = "about:blank"

        def __init__(self):
            self.pages = []
            self.cookie_filters = []
            self.sessions = []

        def clear_cookies(self, **filters):
            self.cookie_filters.append(filters)

        def new_page(self):
            page = self.Page()
            self.pages.append(page)
            return page

        def new_cdp_session(self, _page):
            session = CDPSession()
            self.sessions.append(session)
            return session

    context = Context()
    monkeypatch.setenv("FLIGHTBOT_GACA_PROXY_SESSION", "session-one")

    assert gaca_normal_browser._sync_proxy_session_state(
        context, tmp_path) is True
    assert len(context.cookie_filters) == 1
    assert len(context.sessions[0].calls) == 3
    assert context.sessions[0].detached is True

    assert gaca_normal_browser._sync_proxy_session_state(
        context, tmp_path) is False
    assert len(context.cookie_filters) == 1

    monkeypatch.setenv("FLIGHTBOT_GACA_PROXY_SESSION", "session-two")
    assert gaca_normal_browser._sync_proxy_session_state(
        context, tmp_path) is True
    assert len(context.cookie_filters) == 2


def test_gaca_proxy_rotation_updates_shared_session_file(
        tmp_path, monkeypatch):
    session_file = tmp_path / "proxy-session"
    session_file.write_text("old-session-value", encoding="ascii")
    monkeypatch.setenv(
        "FLIGHTBOT_GACA_PROXY_SESSION_FILE", str(session_file))
    before = gaca_normal_browser._proxy_session_fingerprint()

    assert gaca_normal_browser.rotate_proxy_session() is True
    after = gaca_normal_browser._proxy_session_fingerprint()

    assert after
    assert after != before
    if os.name != "nt":
        assert session_file.stat().st_mode & 0o777 == 0o600


def test_gaca_normalizes_saudia_and_local_mobile_number():
    assert portal_automation._gaca_airline_label({
        "airline_code": "SV", "airline_name": "Saudia"
    }) == "Saudi Arabian Airlines"
    assert portal_automation._gaca_mobile({
        "country_code": "+966", "phone": "+966599491494"
    }) == "599491494"


def test_gaca_nafath_walks_tab_and_national_id_screen(monkeypatch):
    class Page:
        url = "https://myeservices.gaca.gov.sa/eservices/login"

        def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    filled = []
    updates = []

    def click(_page, names):
        if any("Nafath" in name for name in names):
            page.url = (
                "https://myeservices.gaca.gov.sa/eservices/login/nafath")
            return True
        if any("Login" in name for name in names):
            page.url = "https://myeservices.gaca.gov.sa/eservices/dashboard"
            return True
        return False

    monkeypatch.setattr(
        portal_automation, "_is_gaca_login_page",
        lambda current: "/login" in current.url)
    monkeypatch.setattr(portal_automation, "_click", click)
    monkeypatch.setattr(
        portal_automation, "_fill",
        lambda _page, labels, value, **_kwargs:
        filled.append((labels, value)) or True)
    monkeypatch.setattr(portal_automation, "_body_text", lambda _page: "")
    monkeypatch.setattr(portal_automation, "_page_screenshot", lambda _page: b"")

    state = portal_automation._start_gaca_nafath(
        page, {"national_id": "1108337526"},
        lambda *args: updates.append(args))

    assert state == "authenticated"
    assert filled[-1][1] == "1108337526"
    assert any("Opening GACA" in args[1] for args in updates)


def test_gaca_nafath_explicit_portal_failure_is_safe(monkeypatch):
    class Page:
        url = "https://myeservices.gaca.gov.sa/eservices/login/nafath"

        def wait_for_timeout(self, _milliseconds):
            return None

    updates = []
    monkeypatch.setattr(portal_automation, "_is_gaca_login_page", lambda _page: True)
    monkeypatch.setattr(portal_automation, "_fill", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(portal_automation, "_click", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        portal_automation, "_body_text",
        lambda _page: "Login error! Authentication using Nafath failed")
    monkeypatch.setattr(portal_automation, "_page_screenshot", lambda _page: b"")

    state = portal_automation._start_gaca_nafath(
        Page(), {"national_id": "1108337526"},
        lambda *args: updates.append(args))

    assert state == "failed"
    assert any("retry safely" in args[1] for args in updates)
    assert "outage cooldown" in portal_automation._gaca_login_abort_message(
        Page())


def test_gaca_adapter_walks_all_four_steps(monkeypatch):
    class Page:
        def locator(self, *_args, **_kwargs):
            return object()

        def get_by_role(self, *_args, **_kwargs):
            return object()

        def get_by_label(self, *_args, **_kwargs):
            return object()

        def wait_for_timeout(self, _milliseconds):
            return None

    clicks = []
    fills = []
    selects = []
    selectize = []
    updates = []
    monkeypatch.setattr(
        portal_automation, "_click",
        lambda _page, names: clicks.append(names) or True)
    monkeypatch.setattr(
        portal_automation, "_wait_for_any_visible",
        lambda *_args, **_kwargs: object())
    monkeypatch.setattr(portal_automation, "_visible", lambda _locator: False)
    monkeypatch.setattr(
        portal_automation, "_fill",
        lambda _page, labels, value: fills.append((labels, value)) or True)
    monkeypatch.setattr(
        portal_automation, "_select",
        lambda _page, labels, choices, **_kwargs:
        selects.append((labels, choices)) or True)
    monkeypatch.setattr(
        portal_automation, "_select_gaca_gender",
        lambda _page, _payload: True)
    monkeypatch.setattr(
        portal_automation, "_select_gaca_dropdown",
        lambda _page, label, value: selects.append(([label], [value])) or True)
    monkeypatch.setattr(
        portal_automation, "_select_gaca_category_tree",
        lambda _page, payload: (
            selects.append((["main"], ["Baggage"])) or
            ("On Board Services", "Entertainment Services", "In- flight Screens")))
    monkeypatch.setattr(
        portal_automation, "_selectize_by_label",
        lambda _page, label, query, choices:
        selectize.append((label, query, choices)) or True)

    payload = {
        "incident": "The seat-back entertainment screen was broken.",
        "first_name": "Mansour", "middle_name": "Albu",
        "last_name": "Asais", "email": "m@example.com",
        "phone": "599491494", "country_code": "+966",
        "national_id": "1108337526", "origin": "RUH",
        "destination": "AHB", "airline_code": "SV",
        "airline_name": "Saudia", "flight_date": "2026-07-05",
        "flight_number": "SV1671", "ticket_number": "065-2200278935",
        "pnr": "7V5F9V", "airline_reference": "CAS-123456",
        "airline_complaint_date": "2026-07-15",
        "description": "The screen was broken. " * 10,
        "attachments": [],
    }
    portal_automation._prepare_gaca(
        Page(), payload, lambda stage, message, *_args:
        updates.append((stage, message)))

    assert len(clicks) == 4  # Apply Now, then Next through steps 1-3.
    assert any(labels == ["main"] for labels, _choices in selects)
    assert payload.get("selected_complaint_category") == (
        "On Board Services › Entertainment Services › In- flight Screens")
    assert any(labels == ["airline complaint number",
                          "complaint number with the air carrier"]
               and value == "CAS-123456" for labels, value in fills)
    assert [item[0] for item in selectize] == [
        r"country\s*code", r"flight\s*from", r"flight\s*to"]
    assert any("step 4 of 4" in message for _stage, message in updates)


def test_claude_baggage_category_maps_to_saudia_quality_option():
    assert portal_automation._saudia_complaint_category({
        "incident": "My property was damaged during handling.",
        "ai_analysis": {"category": "baggage"},
    }) == "Quality of services"


def test_gaca_category_honors_explicit_baggage_delay_priority():
    payload = {
        "incident": "My baggage was delayed and delivered with damage.",
        "gaca_category": {
            "main": "Baggage Services",
            "sub": "Baggage Delay",
            "detail": "",
        },
    }

    assert portal_automation._gaca_categories(payload) == (
        "Baggage Services", "Baggage Delay", "")


def test_reference_is_extracted_from_official_confirmation_text():
    assert _extract_reference(
        "Thank you. Your complaint reference number is CAS-12345678.") == (
            "CAS-12345678")


def test_reference_is_extracted_from_confirmation_url():
    assert _extract_reference_from_url(
        "https://help.flyadeal.com/hc/en-us/requests/123456") == "123456"


def test_invalid_portal_field_is_screenshot_and_filled_from_telegram(monkeypatch):
    class Control:
        value = ""

        def evaluate(self, script):
            if "tagName" in script:
                return "input"
            if "validationMessage" in script:
                return "Please fill out this field."
            return "Passport number"

        def get_attribute(self, _name):
            return "text"

        def fill(self, value):
            self.value = value

    class Page:
        url = "https://official.example/form"

        def wait_for_timeout(self, _milliseconds):
            pass

    control = Control()
    challenges = []
    updates = []

    def verification(challenge):
        challenges.append(challenge)
        return "P1234567"

    monkeypatch.setattr(portal_automation, "_VERIFICATION_HANDLER", verification)
    monkeypatch.setattr(
        portal_automation, "_invalid_controls",
        lambda _page: [] if control.value else [control])
    monkeypatch.setattr(
        portal_automation, "_control_screenshot",
        lambda _page, _control: b"portal-screenshot")

    changed, cancelled = portal_automation._resolve_invalid_fields(
        Page(), lambda phase, message: updates.append((phase, message)))
    assert changed is True
    assert cancelled is False
    assert control.value == "P1234567"
    assert challenges[0]["kind"] == "field_input"
    assert challenges[0]["image"] == b"portal-screenshot"
    assert "Passport number" in challenges[0]["message"]
    assert updates[-1][0] == "filling"


def test_saved_email_repairs_invalid_control_without_asking_telegram(monkeypatch):
    class Control:
        value = ""

        def evaluate(self, script):
            if "tagName" in script:
                return "input"
            if "validationMessage" in script:
                return "Please fill out this field."
            return "Email"

        def get_attribute(self, name):
            return "email" if name in {"type", "name"} else ""

        def fill(self, value):
            self.value = value

    class Page:
        def wait_for_timeout(self, _milliseconds):
            pass

    control = Control()
    monkeypatch.setattr(
        portal_automation, "_invalid_controls",
        lambda _page: [] if control.value else [control])
    monkeypatch.setattr(
        portal_automation, "_control_label", lambda _control: "Email*")
    monkeypatch.setattr(
        portal_automation, "_VERIFICATION_HANDLER",
        lambda _challenge: pytest.fail("Telegram must not be asked for saved email"))

    changed, cancelled = portal_automation._resolve_invalid_fields(
        Page(), lambda *_args: None,
        payload={"email": "saved@example.com"})
    assert (changed, cancelled) == (True, False)
    assert control.value == "saved@example.com"


def test_confirmation_wait_never_clicks_submit_again(monkeypatch):
    ticks = iter([0.0, 1.0, 13.0, 21.0])

    class Page:
        url = "https://official.example/form"

        def is_closed(self):
            return False

    clicked = []
    monkeypatch.setattr(portal_automation.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(portal_automation.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(portal_automation, "_request_blocked", lambda _page: False)
    monkeypatch.setattr(portal_automation, "_body_text", lambda _page: "Still processing")
    monkeypatch.setattr(portal_automation, "_needs_human_step", lambda _page: "")
    monkeypatch.setattr(portal_automation, "_page_screenshot", lambda _page: b"shot")
    monkeypatch.setattr(
        portal_automation, "_click",
        lambda _page, names: clicked.extend(names) or True)

    result = portal_automation._await_confirmation(
        Page(), Page.url, lambda *_args: None, timeout_seconds=20)
    assert result.status == "confirmation_unknown"
    assert clicked == []


def test_saudia_backend_acceptance_requires_a_real_reference():
    accepted = portal_automation._saudia_submission_result({
        "seen": True, "status": 200,
        "json": {"data": {"complaintNumber": "CAS-44556677"}},
    })
    assert accepted.status == "submitted"
    assert accepted.reference == "CAS-44556677"

    pending = portal_automation._saudia_submission_result({
        "seen": True, "status": 200, "json": {"data": {"saved": True}},
    })
    assert pending.status == "accepted_pending_reference"
    assert pending.reference == ""

    internal_id = portal_automation._saudia_submission_result({
        "seen": True, "status": 200,
        "json": {"data": {"requestId": 1752497514}},
    })
    assert internal_id.status == "accepted_pending_reference"
    assert internal_id.reference == ""


def test_submission_reference_rejects_bare_backend_numbers():
    assert portal_automation._submission_reference({
        "data": {"requestId": 1752497514,
                 "complaintNumber": 1752497514},
    }) == ""
    assert portal_automation._submission_reference({
        "data": {"complaintNumber": "C_2761389"},
    }) == "C_2761389"


def test_gaca_internal_survey_details_id_is_not_a_public_reference():
    assert portal_automation._extract_gaca_reference_from_url(
        "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
        "survey?detailsId=4701857") == ""
    assert portal_automation._extract_gaca_reference_from_url(
        "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
        "survey?reference=C076239") == "C076239"
    assert portal_automation._extract_gaca_reference_from_url(
        "https://myeservices.gaca.gov.sa/eservices/eservice/"
        "details?detailsId=2642710") == ""


def test_gaca_category_native_select_is_verified_after_selection(monkeypatch):
    class Checked:
        def inner_text(self):
            return "Entertainment Services"

    class Control:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def locator(self, selector):
            assert selector == "option:checked"
            return Checked()

        def input_value(self):
            return "entertainment-services"

    class Page:
        def __init__(self):
            self.control = Control()

        def locator(self, selector):
            assert selector == "select#subCategorySelect"
            return self.control

        def wait_for_timeout(self, _value):
            pass

    monkeypatch.setattr(
        portal_automation, "_gaca_live_select_options",
        lambda _page, _select_id: ["Meals", "Entertainment Services"])
    monkeypatch.setattr(
        portal_automation, "_select_native_option",
        lambda _control, choices: choices == [r"^Entertainment\ Services$"])

    assert portal_automation._select_gaca_category_value(
        Page(), "subCategorySelect", "Entertainment Services"
    ) == "Entertainment Services"


def test_gaca_split_verification_code_fills_each_digit(monkeypatch):
    class Field:
        def __init__(self):
            self.value = ""

        def is_visible(self):
            return True

        def fill(self, value):
            self.value = value

    class Fields:
        def __init__(self):
            self.items = [Field() for _ in range(4)]
            self.first = self.items[0]

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        url = "https://myeservices.gaca.gov.sa/eservices/public/qpe/verification/email"

        def __init__(self):
            self.fields = Fields()

        def get_by_role(self, *_args, **_kwargs):
            return self.fields

        def locator(self, _selector):
            return self.fields

        def wait_for_timeout(self, _value):
            pass

    page = Page()
    monkeypatch.setattr(
        portal_automation, "_VERIFICATION_HANDLER",
        lambda _challenge: "1078")
    monkeypatch.setattr(portal_automation, "_click", lambda *_args: True)

    assert portal_automation._solve_otp(
        page, lambda *_args: None) is True
    assert [field.value for field in page.fields.items] == list("1078")


def test_gaca_normal_browser_uses_os_input_for_email_code(monkeypatch):
    class Field:
        def __init__(self):
            self.value = ""

        def is_visible(self):
            return True

        def fill(self, value):
            raise AssertionError("Playwright must not type the GACA OTP")

    class Fields:
        def __init__(self):
            self.items = [Field() for _ in range(4)]
            self.first = self.items[0]

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        url = (
            "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
            "verification/email"
        )

        def __init__(self):
            self.fields = Fields()

        def get_by_role(self, *_args, **_kwargs):
            return self.fields

        def locator(self, _selector):
            return self.fields

        def wait_for_timeout(self, _value):
            pass

    page = Page()
    used = []
    monkeypatch.setenv("FLIGHTBOT_GACA_OS_INPUT", "1")
    monkeypatch.setattr(
        portal_automation, "_VERIFICATION_HANDLER",
        lambda _challenge: "9534")
    monkeypatch.setattr(
        portal_automation.gaca_normal_browser,
        "physical_type_otp",
        lambda _page, fields, code, verify: used.append(
            (fields, code, verify)))

    assert portal_automation._solve_otp(
        page, lambda *_args: None) is True
    assert used and used[0][1] == "9534"


def test_saudia_backend_rejection_and_unconfirmed_timeout_are_failures():
    rejected = portal_automation._saudia_submission_result({
        "seen": True, "status": 500, "json": {"data": None},
    })
    assert rejected.status == "error"

    result = portal_automation._await_confirmation(
        object(), "https://www.saudia.com/en-SA/forms/contact-form",
        lambda *_args: None,
        payload={"airline_code": "SV"}, timeout_seconds=0,
        submission_capture={})
    assert result.status == "error"


def test_gaca_post_capture_distinguishes_rejection_and_acceptance():
    class Request:
        method = "POST"

    class Response:
        url = ("https://myeservices.gaca.gov.sa/eservices/public/qpe/"
               "complaint-airline/step4")
        request = Request()
        status = 200
        headers = {}

        def json(self):
            raise ValueError

        def text(self):
            return "Error! Security check failed, please try later"

    rejected_capture = {}
    portal_automation._capture_gaca_response(Response(), rejected_capture)
    rejected = portal_automation._gaca_submission_result(
        rejected_capture, {"airline_reference": "C_2760788"})
    assert rejected.status == "verification_expired"

    accepted = portal_automation._gaca_submission_result({
        "seen": True,
        "status": 200,
        "text": "Complaint successfully submitted. Reference GACA-987654",
    }, {"airline_reference": "C_2760788"})
    assert accepted.status == "submitted"
    assert accepted.reference == "GACA-987654"

    echoed_airline_case = portal_automation._gaca_submission_result({
        "seen": True,
        "status": 200,
        "text": "Airline Complaint Number C_2760788",
    }, {"airline_reference": "C_2760788"})
    assert echoed_airline_case is None


def test_gaca_uses_one_solver_path_instead_of_native_then_solver(monkeypatch):
    monkeypatch.setattr(
        portal_automation, "_recaptcha_challenge",
        lambda _page: {"kind": "recaptcha", "is_v3": True})
    monkeypatch.setattr(
        portal_automation, "_recaptcha_page_context",
        lambda _page, _site_key: {"is_v3": True})
    monkeypatch.setattr(
        portal_automation, "_CAPTCHA_SOLVER",
        lambda _challenge: {"token": "solved"})
    monkeypatch.setenv("FLIGHTBOT_GACA_NATIVE_RECAPTCHA", "1")

    assert portal_automation._use_gaca_native_recaptcha(
        object(), is_gaca=True, attempt=0) is True
    assert portal_automation._use_gaca_native_recaptcha(
        object(), is_gaca=True, attempt=1) is False
    assert portal_automation._use_gaca_native_recaptcha(
        object(), is_gaca=False, attempt=0) is False

    monkeypatch.setenv("FLIGHTBOT_GACA_NATIVE_RECAPTCHA", "0")
    assert portal_automation._use_gaca_native_recaptcha(
        object(), is_gaca=True, attempt=0) is False


def test_gaca_success_without_reference_is_not_submitted_twice():
    accepted = portal_automation._gaca_submission_result({
        "seen": True,
        "status": 302,
        "location": "/complaint-airline/submission-success",
    })
    assert accepted.status == "accepted_pending_reference"
    assert "will not submit a duplicate" in accepted.message


def test_gaca_visible_success_waits_for_real_sms_reference(monkeypatch):
    class Hidden:
        first = None

        def count(self):
            return 0

    class Page:
        url = (
            "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
            "survey?detailsId=4701857"
        )

        def is_closed(self):
            return False

        def get_by_role(self, *_args, **_kwargs):
            return Hidden()

    monkeypatch.setattr(
        portal_automation, "_request_blocked", lambda _page: False)
    monkeypatch.setattr(
        portal_automation, "_body_text",
        lambda _page: "Your request has been successfully submitted")
    monkeypatch.setattr(
        portal_automation, "_page_screenshot", lambda _page: b"success")

    result = portal_automation._await_confirmation(
        Page(),
        "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
        "complaint-airline/step4?detailsId=2642710",
        lambda *_args: None,
        payload={"kind": "gaca", "airline_code": "SV"},
        timeout_seconds=5,
        submission_capture={},
    )
    assert result.status == "accepted_pending_reference"
    assert result.reference == ""


def test_gaca_home_after_email_verification_waits_for_sms_reference(
        monkeypatch):
    class Control:
        def __init__(self, *, otp=False):
            self.otp = otp

    class Page:
        url = (
            "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
            "verification/email"
        )

        def is_closed(self):
            return False

        def get_by_role(self, *_args, **_kwargs):
            return Control()

        def wait_for_timeout(self, _value):
            pass

    page = Page()
    monkeypatch.setattr(
        portal_automation, "_gaca_submission_result",
        lambda *_args: None)
    monkeypatch.setattr(
        portal_automation, "_request_blocked", lambda _page: False)
    monkeypatch.setattr(
        portal_automation, "_body_text", lambda _page: "")
    monkeypatch.setattr(
        portal_automation, "_visible",
        lambda control: bool(getattr(control, "otp", False)))
    monkeypatch.setattr(
        portal_automation, "_otp_fields",
        lambda _page: Control(otp=True))
    monkeypatch.setattr(
        portal_automation, "_needs_human_step",
        lambda _page: "Enter the OTP")

    def complete_email_verification(_page, _update):
        page.url = "https://myeservices.gaca.gov.sa/eservices/home"
        return True

    monkeypatch.setattr(
        portal_automation, "_wait_for_human_step",
        complete_email_verification)
    monkeypatch.setattr(
        portal_automation, "_VERIFICATION_HANDLER",
        lambda _challenge: "9534")

    result = portal_automation._await_confirmation(
        page,
        "https://myeservices.gaca.gov.sa/eservices/public/qpe/"
        "complaint-airline/step4?detailsId=2642710",
        lambda *_args: None,
        payload={"kind": "gaca", "airline_code": "SV"},
        timeout_seconds=5,
        submission_capture={},
    )

    assert result.status == "accepted_pending_reference"
    assert result.reference == ""


def test_ai_portal_guardrails_block_final_and_security_actions(monkeypatch):
    clicked = []
    filled = []
    monkeypatch.setattr(
        portal_automation, "_click",
        lambda _page, names: clicked.extend(names) or True)
    monkeypatch.setattr(
        portal_automation, "_fill",
        lambda _page, labels, value: filled.append((labels, value)) or True)
    update = lambda *_args: None
    payload = {"email": "passenger@example.com", "pnr": "ABC123"}

    handled, cancelled = portal_automation._apply_ai_decision(
        object(), {
            "state": "ready", "action": "click", "target": "Submit",
            "value": "", "confidence": .99, "summary": "", "user_prompt": "",
        }, payload, update)
    assert (handled, cancelled) == (False, False)
    assert clicked == []

    handled, _ = portal_automation._apply_ai_decision(
        object(), {
            "state": "needs_field", "action": "fill", "target": "OTP code",
            "value": "ABC123", "confidence": .99, "summary": "", "user_prompt": "",
        }, payload, update)
    assert handled is False
    assert filled == []


def test_ai_portal_guardrails_allow_only_known_values_and_safe_navigation(monkeypatch):
    clicked = []
    filled = []
    monkeypatch.setattr(
        portal_automation, "_click",
        lambda _page, names: clicked.extend(names) or True)
    monkeypatch.setattr(
        portal_automation, "_fill",
        lambda _page, labels, value: filled.append((labels, value)) or True)
    update = lambda *_args: None
    payload = {"email": "passenger@example.com", "pnr": "ABC123"}

    invented, _ = portal_automation._apply_ai_decision(
        object(), {
            "state": "needs_field", "action": "fill", "target": "Email",
            "value": "invented@example.com", "confidence": .99,
            "summary": "", "user_prompt": "",
        }, payload, update)
    known, _ = portal_automation._apply_ai_decision(
        object(), {
            "state": "needs_field", "action": "fill", "target": "Email",
            "value": "passenger@example.com", "confidence": .99,
            "summary": "", "user_prompt": "",
        }, payload, update)
    navigated, _ = portal_automation._apply_ai_decision(
        object(), {
            "state": "needs_navigation", "action": "click", "target": "Next",
            "value": "", "confidence": .99, "summary": "", "user_prompt": "",
        }, payload, update)
    assert invented is False
    assert known is True
    assert navigated is True
    assert filled[0][1] == "passenger@example.com"
    assert clicked
