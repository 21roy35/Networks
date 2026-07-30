from datetime import datetime

from flight_bot.airlines import SAUDI_CARRIERS, airline_for_domain
from flight_bot import portal_automation
from flight_bot.linker import link_emails
from flight_bot.mail_client import _search_queries
from flight_bot.parser import BOOKING, parse_email


RIYADH_AIR_ORDER = """
We look forward to welcoming you on board
Order ID: RX12236S4XVH7
Your order’s all set
Hi Mansour,
Please find below a summary of your order, along with flight details.
Order summary
Departs at
22:45
Tue 16 Jun 2026
Jeddah (JED)
King Abdulaziz Airport
Terminal 1
RX - 28
Boeing 787 -9
Operated by Riyadh Air
Flight duration: 1h 45m
Arrives at
00:30
Wed 17 Jun 2026
Riyadh (RUH)
King Khalid Airport
Terminal 3
1 Guest
Mansour Albu Asais
Cabin class: Business - Smart Seat
2 G Baggage
Receipt
Name
Mansour Albu Asais
Order ID
RX12236S4XVH7
Form of payment
Master Card - 9319
Total paid
SAR 2780.70
"""


def test_riyadh_air_domain_is_searched_and_is_a_saudi_carrier():
    code, airline = airline_for_domain("orders.riyadhair.com")

    assert code == "RX"
    assert airline["name"] == "Riyadh Air"
    assert "RX" in SAUDI_CARRIERS
    assert airline["complaint_url"] == (
        "https://rxcreatecase.powerappsportals.com/en-us/Create-Case/"
    )
    assert portal_automation._is_official_url(airline["complaint_url"])
    assert any(
        'FROM "riyadhair.com"' in query
        for query in _search_queries("01-Jan-2026"))


def test_riyadh_air_order_layout_becomes_a_complete_flight():
    parsed = parse_email(
        "<order@orders.riyadhair.com>",
        "All set with your Order RX12236S4XVH7",
        "notifications@orders.riyadhair.com",
        datetime(2026, 6, 14, 15, 55),
        RIYADH_AIR_ORDER,
    )

    assert parsed is not None
    assert parsed.airline_code == "RX"
    assert BOOKING in parsed.kinds
    assert parsed.pnr == "RX12236S4XVH7"
    assert parsed.flight_numbers == ["RX28"]
    assert parsed.flight_date == "2026-06-16"
    assert parsed.origin == "JED"
    assert parsed.destination == "RUH"
    assert parsed.departure == "2026-06-16 22:45"
    assert parsed.arrival == "2026-06-17 00:30"
    assert parsed.passenger == "Mansour Albu Asais"
    assert parsed.cabin_class == "Business"
    assert parsed.seat == "2G"
    assert parsed.payment_method == "Mastercard •••• 9319"

    record = {
        key: value for key, value in vars(parsed).items()
        if key != "body_text"
    }
    record["db_id"] = 1
    record["date"] = parsed.date.isoformat()
    flights = link_emails([record])

    assert len(flights) == 1
    assert flights[0]["flight_key"] == "RX12236S4XVH7|RX28|2026-06-16"
    assert flights[0]["arrival"] == "2026-06-17 00:30"


def test_riyadh_air_helpers_normalize_case_fields_and_confirmation():
    assert portal_automation._riyadh_air_phone({
        "country_code": "+966", "phone": "599491494",
    }) == "+966599491494"
    assert portal_automation._riyadh_air_issue_date(
        "2026-06-16") == "6/16/2026"
    assert portal_automation._riyadh_air_hidden_issue_date(
        "2026-06-16") == "2026-06-15T21:00:00.0000000Z"
    english = portal_automation._riyadh_air_case_description({
        "incident": (
            "A staff member opened the restroom door, there was no amenity "
            "kit, the food was cold, and the Business seat door was broken. "
            "I request fair financial compensation."
        ),
    })
    assert english == (
        "Restroom privacy breach; no amenity kit; cold food; broken Business "
        "seat door. Request compensation."
    )
    assert len(english) == 100
    assert portal_automation._extract_riyadh_air_reference(
        "Your case number is 23062606433818524"
    ) == "23062606433818524"

    accepted = portal_automation._riyadh_air_submission_result({
        "seen": True,
        "status": 200,
        "text": (
            "Thank you. Your case number is 23062606433818524 "
            "and was created successfully."
        ),
    })
    assert accepted.status == "submitted"
    assert accepted.reference == "23062606433818524"

    rejected = portal_automation._riyadh_air_submission_result({
        "seen": True,
        "status": 200,
        "text": "The CAPTCHA code is incorrect.",
    })
    assert rejected.status == "verification_expired"


def test_riyadh_air_form_and_image_captcha_are_completed_deterministically(
        monkeypatch):
    class Control:
        def __init__(self, *, value="", visible=True, image=b""):
            self.first = self
            self.value = value
            self.visible = visible
            self.image = image
            self.pressed = []

        def count(self):
            return 1

        def is_visible(self):
            return self.visible

        def fill(self, value):
            self.value = value

        def evaluate(self, _script, value):
            self.value = value

        def press(self, key):
            self.pressed.append(key)

        def input_value(self):
            return self.value

        def screenshot(self, **_kwargs):
            return self.image

    class Empty(Control):
        def __init__(self):
            super().__init__(visible=False)

        def count(self):
            return 0

    controls = {
        "#rx_guestname": Control(),
        "#rx_orderid": Control(),
        "#rx_emailid": Control(),
        "#rx_phonenumber": Control(),
        "#rx_issuedescription": Control(),
        "#rx_dateofissue_datepicker_description": Control(),
        "#rx_dateofissue": Control(visible=False),
        "input[id^='frm_pref_']": Control(value=""),
        "#InsertButton": Control(),
    }
    captcha_image = Control(image=b"image-png")
    captcha_field = Control()

    class Page:
        frames = []
        url = (
            "https://rxcreatecase.powerappsportals.com/en-us/Create-Case/")

        def locator(self, selector):
            if selector.startswith("img[src*='captcha'"):
                return captcha_image
            if selector.startswith("input[type='text'][name*='captcha'"):
                return captcha_field
            return controls.get(selector, Empty())

        def evaluate(self, _script):
            return False

        def screenshot(self, **_kwargs):
            return b"page"

    payload = {
        "booking_passenger_name": "Mansour Albu Asais",
        "passenger_name": "Mansour Saeed Albu Asais",
        "pnr": "RX12236S4XVH7",
        "email": "passenger@example.com",
        "country_code": "+966",
        "phone": "599491494",
        "description": (
            "I was in the restroom when a staff member opened the door on me. "
            "No amenity kit was provided in Business Class, the food was cold, "
            "and the Business Class seat door was broken. Please write the "
            "complaint in Arabic and request compensation."
        ),
        "incident": (
            "Was in restroom and staff opened the door on me; no amenity kit "
            "in business; food was served cold; seat door was broken. Please "
            "write the complaint in Arabic."
        ),
        "flight_date": "2026-06-16",
    }
    updates = []
    page = Page()
    portal_automation._prepare_riyadh_air(
        page, payload, lambda *args: updates.append(args))

    assert controls["#rx_guestname"].value == "Mansour Albu Asais"
    assert controls["#rx_orderid"].value == "RX12236S4XVH7"
    assert controls["#rx_phonenumber"].value == "+966599491494"
    assert controls["#rx_dateofissue_datepicker_description"].value == "6/16/2026"
    assert controls["#rx_dateofissue"].value == (
        "2026-06-15T21:00:00.0000000Z")
    assert controls["#rx_issuedescription"].value == (
        "انتهاك خصوصيتي بدورة المياه؛ لا حقيبة مستلزمات؛ الطعام بارد؛ "
        "باب المقعد مكسور. أطلب تعويضًا."
    )
    assert len(controls["#rx_issuedescription"].value) <= 100
    assert portal_automation._pending_captcha_kind(page) == "text"

    monkeypatch.setattr(
        portal_automation, "_CAPTCHA_SOLVER",
        lambda challenge: (
            {"text": "AbC91"}
            if challenge["kind"] == "image_captcha" else None
        ),
    )
    monkeypatch.setattr(portal_automation, "_VERIFICATION_HANDLER", None)
    assert portal_automation._solve_text_captcha(
        page, lambda *args: updates.append(args))
    assert captcha_field.value == "AbC91"
    assert portal_automation._pending_captcha_kind(page) == ""
