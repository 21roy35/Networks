from flight_bot.linker import link_emails
from flight_bot.parser import BOOKING, CANCELLATION, CHECKIN, ETICKET, RECEIPT


def _segment(number: str, date: str, departure: str, arrival: str) -> dict:
    return {
        "origin": "JED", "destination": "AHB", "date": date,
        "dep_time": departure, "arr_time": arrival,
        "flight_number": number, "label": None,
    }


def test_cancellation_is_scoped_to_exact_flight_within_rebooked_pnr():
    emails = [
        {
            "db_id": 1, "pnr": "8A96LW",
            "kinds": [ETICKET, RECEIPT, BOOKING],
            "ticket_numbers": ["0650000000001"],
            "flight_numbers": [], "segments": [],
            "passenger": "Muhannad Alqahtani",
        },
        {
            "db_id": 2, "pnr": "8A96LW",
            "date": "2026-07-16T11:35:46",
            "kinds": [CANCELLATION, BOOKING],
            "flight_numbers": ["SV1650"], "flight_date": "2026-07-17",
            "segments": [_segment("SV1650", "2026-07-17", "17:50", "19:10")],
        },
        {
            "db_id": 3, "pnr": "8A96LW",
            "date": "2026-07-16T15:15:41",
            "kinds": [CHECKIN, ETICKET, BOOKING],
            "flight_numbers": ["SV1642"], "flight_date": "2026-07-18",
            "segments": [_segment("SV1642", "2026-07-18", "02:45", "04:05")],
        },
    ]

    flights = {flight["flight_number"]: flight
               for flight in link_emails(emails)}

    assert flights["SV1650"]["cancelled"] is True
    assert CANCELLATION in flights["SV1650"]["kinds"]
    assert flights["SV1650"]["email_ids"] == [1, 2]
    assert flights["SV1642"]["cancelled"] is False
    assert CANCELLATION not in flights["SV1642"]["kinds"]
    assert flights["SV1642"]["email_ids"] == [1, 3]
