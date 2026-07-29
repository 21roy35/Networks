from flight_bot.complaints import gaca_complaint


def test_gaca_text_omits_natural_date_and_other_structured_fields():
    flight = {
        "airline_code": "SV",
        "airline_name": "Saudia",
        "flight_number": "SV1671",
        "flight_date": "2026-07-05",
        "origin": "RUH",
        "destination": "AHB",
        "pnr": "7V5F9V",
        "ticket_numbers": ["065-2200278935"],
    }
    result = gaca_complaint(
        flight,
        {"full_name": "Mansour Saeed Albu Asais", "national_id": "1108337526"},
        incident=(
            "The screen did not work throughout flight SV1671 on 5 July 2026. "
            "Booking reference 7V5F9V. I could not use the entertainment system."
        ),
        airline_reference="C_2760788",
        airline_complaint_date="2026-07-15",
        requested_remedy="I request fair compensation for the failed service.",
    )

    body = result["body"]
    for repeated in (
        "SV1671",
        "5 July 2026",
        "2026-07-05",
        "7V5F9V",
        "C_2760788",
        "1108337526",
    ):
        assert repeated not in body
    assert "I could not use the entertainment system" in body
