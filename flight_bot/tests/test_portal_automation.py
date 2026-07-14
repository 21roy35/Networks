import pytest

from flight_bot import portal_automation
from flight_bot.airlines import AIRLINES
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


def test_ai_analysis_augments_letter_but_preserves_original_incident():
    original = "My seat was broken and the screen did not work."
    payload = complaint_payload(
        sample_flight(), profile(), "airline", original,
        ai_analysis={
            "category": "seat", "summary": "Seat and screen were unusable.",
            "facts": ["The seat was broken", "The screen did not work"],
            "evidence_observations": ["A seat component appears displaced"],
            "requested_remedy": "Investigate and provide applicable remedies.",
        })
    assert payload["incident"] == original
    assert payload["ai_analysis"]["category"] == "seat"
    assert "Passenger's original statement" in payload["description"]
    assert original in payload["description"]


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
        "https://booking-uat.dcloud.saudia.com/forms/contact-form")
    assert _is_official_url(AIRLINES["SV"]["complaint_url"])


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
            if selector == "mat-option":
                return Options()
            raise AssertionError(selector)

        def wait_for_timeout(self, _milliseconds):
            pass

    assert portal_automation._select(
        Page(), ["service type"], ["travel complaint or compliment"])
    assert selected == ["Travel complaint or compliment"]


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
