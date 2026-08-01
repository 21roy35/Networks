from flight_bot import db
from flight_bot.gaca_status import (
    GACA_API_ROOT,
    GacaStatusError,
    check_gaca_case,
    extract_gaca_details_url,
    interpret_gaca_remediation,
    normalize_gaca_phone,
    requires_airline_complaint,
)


class Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = value if isinstance(value, str) else ""

    def json(self):
        return self.value


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.proxies = {}

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return next(self.responses)


def test_gaca_details_link_is_strict_and_phone_is_normalized():
    assert extract_gaca_details_url(
        "للتفاصيل: https://pxpticket.gaca.gov.sa/"
    ) == "https://pxpticket.gaca.gov.sa/"
    assert extract_gaca_details_url(
        "Details: https://pxpticket.gaca.gov.sa.evil.example/"
    ) == ""
    assert normalize_gaca_phone("+966", "599491494") == "+966599491494"
    assert normalize_gaca_phone("+966", "+966599491494") == "+966599491494"
    assert requires_airline_complaint(
        "يجب أولاً تقديم الشكوى لدى الناقل الجوي، ثم الانتظار 7 أيام.")
    assert not requires_airline_complaint(
        "The airline must pay compensation within seven days.")


def test_gaca_remediation_requires_explicit_verified_instruction():
    prerequisite = interpret_gaca_remediation(
        "The supplied reference is not a complaint number. You must first "
        "file a complaint with the airline and wait seven days.",
        case_status="canceled",
    )
    assert prerequisite.action == "airline_prerequisite"
    assert prerequisite.automatic is True

    category = interpret_gaca_remediation(
        "This case was filed under the wrong category. Please resubmit under "
        "category: Baggage Damage.",
        case_status="rejected",
    )
    assert category.action == "correct_category"
    assert "Baggage Damage" in category.suggested_category

    information = interpret_gaca_remediation(
        "Case status: needs_information",
        case_status="needs_information",
        data={
            "notesRegardingAdditionalInfoFromTheTravel":
                "Upload the baggage report and damage photographs.",
        },
    )
    assert information.action == "provide_information"
    assert "baggage report" in information.requested_information

    plain_closure = interpret_gaca_remediation(
        "The complaint is closed.",
        case_status="closed",
    )
    assert plain_closure.action == "review"


def test_gaca_checker_solves_captcha_relays_otp_and_reads_solution():
    session = Session([
        Response({"message": "verification required"}, status=401),
        Response("otp-request-123"),
        Response(True),
        Response({
            "status": 200,
            "data": {
                "title": "C076574",
                "status": 850980009,
                "standardReplyDetails": (
                    "The supplied airline reference is not a complaint number."
                ),
                "category": "On Board Services",
                "subCategory": "Crew Behavior",
            },
        }),
    ])
    challenges = []
    verifications = []
    updates = []

    result = check_gaca_case(
        "C076574",
        "+966599491494",
        captcha_solver=lambda challenge: (
            challenges.append(challenge) or {"token": "captcha-token"}
        ),
        verification_handler=lambda challenge: (
            verifications.append(challenge) or "1234"
        ),
        session=session,
        update=lambda *args: updates.append(args),
    )

    assert result.status == "rejected"
    assert "not a complaint number" in result.response_text
    assert challenges[0]["kind"] == "recaptcha"
    assert verifications[0]["kind"] == "otp"
    assert session.requests[1][1] == (
        f"{GACA_API_ROOT}/PxpTicket/SendOtpToCaseOwner")
    assert session.requests[1][2]["headers"]["reCAPTCHA-Token"] == (
        "captcha-token")
    assert session.requests[2][2]["json"] == {
        "id": "otp-request-123", "otp": "1234",
    }
    assert session.requests[3][2]["params"] == {
        "phoneNumber": "+966599491494",
    }
    assert session.proxies == {}
    assert [item[0] for item in updates] == [
        "checking", "verification", "otp", "checking",
    ]


def test_gaca_checker_uses_verified_read_only_record_without_otp():
    session = Session([Response({
        "status": 200,
        "data": {
            "title": "C076100",
            "status": 850980013,
            "statusDescription": "In Progress",
            "ticketNumber": "CAS-514645-V1S6J8",
            "category": "On Board Services",
            "subCategory": "Entertainment Services",
        },
    })])
    solved = []
    verified = []

    result = check_gaca_case(
        "C076100",
        "+966599491494",
        captcha_solver=lambda challenge: solved.append(challenge),
        verification_handler=lambda challenge: verified.append(challenge),
        session=session,
        proxy_url="http://127.0.0.1:18887",
    )

    assert result.status == "in_progress"
    assert "CAS-514645-V1S6J8" in result.response_text
    assert solved == []
    assert verified == []
    assert [item[0] for item in session.requests] == ["GET"]
    assert session.proxies == {
        "http": "http://127.0.0.1:18887",
        "https": "http://127.0.0.1:18887",
    }


def test_gaca_checker_routes_all_api_calls_through_configured_proxy():
    session = Session([
        Response({"message": "verification required"}, status=401),
        Response({"message": "under review"}, status=401),
    ])
    result = check_gaca_case(
        "C076100",
        "+966599491494",
        captcha_solver=lambda _challenge: {"token": "captcha-token"},
        verification_handler=lambda _challenge: "unused",
        session=session,
        proxy_url="http://127.0.0.1:18887",
    )
    assert result.status == "in_progress"
    assert session.proxies == {
        "http": "http://127.0.0.1:18887",
        "https": "http://127.0.0.1:18887",
    }


def test_gaca_checker_reports_under_review_without_requesting_otp():
    session = Session([
        Response({"message": "verification required"}, status=401),
        Response({"message": "under review"}, status=401),
    ])
    verified = []
    result = check_gaca_case(
        "C076100",
        "+966599491494",
        captcha_solver=lambda _challenge: {"token": "captcha-token"},
        verification_handler=lambda challenge: verified.append(challenge),
        session=session,
    )
    assert result.status == "in_progress"
    assert verified == []


def test_gaca_checker_rejects_untrusted_reference_before_network():
    try:
        check_gaca_case(
            "not-a-case",
            "+966599491494",
            captcha_solver=lambda _challenge: {"token": "unused"},
            verification_handler=lambda _challenge: "1234",
            session=Session([]),
        )
    except GacaStatusError as exc:
        assert "valid GACA" in str(exc)
    else:
        raise AssertionError("invalid GACA reference was accepted")


def test_gaca_status_queue_is_durable_and_uses_latest_sms(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    airline_id = db.begin_complaint(
        "RX|28|2026-06-16", "airline", "Airline complaint", "Incident")
    db.finish_complaint(airline_id, "submitted", "RX-CASE-1")
    gaca_id = db.begin_complaint(
        "RX|28|2026-06-16", "gaca", "GACA complaint", "Incident",
        parent_complaint_id=airline_id,
    )
    db.finish_complaint(gaca_id, "submitted", "C076574")
    sms_id, _created = db.save_sms_message({
        "fingerprint": "gaca-status-sms-10",
        "sender": "GACA CARE",
        "received_at": "2026-07-30T09:16:00+03:00",
        "body": "Closed C076574. Details: https://pxpticket.gaca.gov.sa/",
    })

    first = db.queue_gaca_status_check(
        "C076574", "https://pxpticket.gaca.gov.sa/",
        trigger_sms_id=sms_id, urgent=True)
    duplicate = db.queue_gaca_status_check(
        "C076574", "https://pxpticket.gaca.gov.sa/",
        trigger_sms_id=sms_id, urgent=True)
    assert first == duplicate
    claimed = db.claim_due_gaca_status_check()
    assert claimed["complaint_id"] == gaca_id
    assert claimed["attempts"] == 1

    db.finish_gaca_status_check(
        first,
        case_status="rejected",
        response_text="The airline reference is invalid.",
        response_summary="Rejected because the airline reference was invalid.",
    )
    stored = db.list_gaca_status_checks()[0]
    assert stored["status"] == "checked"
    assert stored["case_status"] == "rejected"
    assert stored["next_attempt_at"] is None
