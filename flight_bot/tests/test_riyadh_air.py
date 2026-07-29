from datetime import datetime

from flight_bot.airlines import SAUDI_CARRIERS, airline_for_domain
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
