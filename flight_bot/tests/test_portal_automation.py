import re

import pytest

from flight_bot import portal_automation
from flight_bot.airlines import AIRLINES
from flight_bot.complaints import complaint_payload, missing_portal_fields
from flight_bot.portal_automation import (_extract_reference,
                                          _extract_reference_from_url, _is_official_url)


def sample_flight():
    return {
        "flight_key": "ABC123|SV100|2026-06-01",
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
    assert payload["flight_key"] == "ABC123|SV100|2026-06-01"
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
    assert payload["description"].startswith(original)
    assert "financial compensation" in payload["description"]
    assert not payload["description"].startswith("Dear")


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

        def evaluate(self, script, *args):
            if script == "navigator.userAgent":
                return "Modern Browser"
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
    assert page.waits == [500]
    assert updates[0][0] == "verification"
    assert updates[-1][0] == "filling"


def test_2captcha_prefers_live_widget_and_keeps_enterprise_data_s():
    class Anchor:
        first = None

        def __init__(self, visible):
            self.visible = visible
            self.first = self

        def count(self):
            return int(self.visible is not None)

        def is_visible(self):
            return bool(self.visible)

    class Frame:
        def __init__(self, site_key, visible, data_s=""):
            suffix = f"&s={data_s}" if data_s else ""
            self.url = ("https://www.google.com/recaptcha/enterprise/anchor?"
                        f"k={site_key}&size=normal{suffix}")
            self.anchor = Anchor(visible)

        def locator(self, selector):
            assert selector == "#recaptcha-anchor"
            return self.anchor

    class Page:
        url = "https://www.saudia.com/en/forms/complaint-form"
        frames = [Frame("stale-key", False), Frame("live-key", True, "fresh-s")]

        def evaluate(self, script):
            assert script == "navigator.userAgent"
            return "Modern Browser"

    challenge = portal_automation._recaptcha_challenge(Page())
    assert challenge["site_key"] == "live-key"
    assert challenge["is_enterprise"] is True
    assert challenge["data_s"] == "fresh-s"


def test_arabic_saudi_nationality_maps_to_english_portal_option():
    choices, queries = portal_automation._nationality_selection("سعودي")
    assert queries == ["Saudi"]
    assert any(re.search(choice, "Saudi Arabian", re.I) for choice in choices)


def test_early_form_review_defers_captcha_until_final_submit(monkeypatch):
    monkeypatch.setattr(
        portal_automation, "_needs_human_step",
        lambda _page: "Solve the CAPTCHA challenge.")
    monkeypatch.setattr(
        portal_automation, "_pending_captcha_kind", lambda _page: "recaptcha")
    assert portal_automation._wait_for_human_step(
        object(), lambda *_args: None, defer_captcha=True) is True


def test_saudia_submit_retries_fresh_captcha_after_expiry(monkeypatch):
    class Page:
        url = "https://www.saudia.com/en/forms/complaint-form"

        def __init__(self):
            self.waits = []

        def on(self, *_args):
            pass

        def wait_for_timeout(self, value):
            self.waits.append(value)

    results = iter([
        portal_automation.PortalResult("verification_expired", "expired"),
        portal_automation.PortalResult("submitted", "accepted", "C_1234567"),
    ])
    clicks = []
    resets = []
    updates = []
    monkeypatch.setattr(portal_automation, "_wait_for_human_step",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(portal_automation, "_verification_expired",
                        lambda _page: False)
    monkeypatch.setattr(portal_automation, "_body_text", lambda _page: "form")
    monkeypatch.setattr(portal_automation, "_page_screenshot", lambda _page: b"shot")
    monkeypatch.setattr(portal_automation, "_click",
                        lambda *_args: clicks.append(True) or True)
    monkeypatch.setattr(portal_automation, "_reset_recaptcha",
                        lambda _page: resets.append(True))
    monkeypatch.setattr(portal_automation, "_await_confirmation",
                        lambda *_args, **_kwargs: next(results))

    result = portal_automation._submit_with_captcha_recovery(
        Page(), {"airline_code": "SV"},
        lambda *args: updates.append(args))
    assert result.status == "submitted"
    assert result.reference == "C_1234567"
    assert len(clicks) == 2
    assert resets == [True]
    assert any("retrying automatically" in args[1] for args in updates)


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


def test_gaca_normalizes_saudia_and_local_mobile_number():
    assert portal_automation._gaca_airline_label({
        "airline_code": "SV", "airline_name": "Saudia"
    }) == "Saudi Arabian Airlines"
    assert portal_automation._gaca_mobile({
        "country_code": "+966", "phone": "+966599491494"
    }) == "599491494"


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
    assert any("main category" in labels[0] for labels, _choices in selects)
    assert any("sub-subcategory" in labels[0] for labels, _choices in selects)
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


def test_saudia_backend_exposes_validation_reason_and_recovers_captcha():
    captcha = portal_automation._saudia_submission_result({
        "seen": True, "status": 400,
        "json": {"data": None, "message": "Verification expired"},
    })
    assert captcha.status == "verification_expired"

    nationality = portal_automation._saudia_submission_result({
        "seen": True, "status": 400,
        "json": {"data": None, "errors": {"message": "Nationality is required"}},
    })
    assert nationality.status == "error"
    assert "Nationality is required" in nationality.message


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
