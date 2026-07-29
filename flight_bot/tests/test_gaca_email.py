from flight_bot import gaca_email, portal_automation


def _config():
    return {
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "user": "passenger@example.com",
            "password": "app-password",
        },
        "gaca_email": {
            "enabled": True,
            "recipient": "1929@gaca.gov.sa",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
        },
    }


def _payload():
    return {
        "kind": "gaca",
        "passenger_name": "Mansour Example",
        "national_id": "1100000000",
        "phone": "500000000",
        "email": "passenger@example.com",
        "airline_name": "Saudia",
        "flight_number": "SV1674",
        "flight_date": "2026-07-09",
        "route": "AHB > RUH",
        "pnr": "8GAKAN",
        "ticket_number": "065-2200541313",
        "airline_reference": "C_2800039",
        "airline_complaint_date": "2026-07-24",
        "gaca_category": {
            "main": "Baggage Services",
            "sub": "Baggage Delay",
            "detail": "",
        },
        "description": (
            "My checked baggage was delayed and no compensation was offered."
        ),
        "attachments": [],
    }


class EmptySentMailbox:
    def __init__(self, *_args, **_kwargs):
        pass

    def login(self, *_args):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "NO", []

    def logout(self):
        return "BYE", []


class RecordingSMTP:
    messages = []

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def login(self, user, password):
        assert user == "passenger@example.com"
        assert password == "app-password"

    def send_message(self, message):
        self.messages.append(message)
        return {}


def test_gaca_email_contains_structured_fields_and_natural_complaint():
    message = gaca_email.compose_gaca_email(
        _payload(), "job-123", _config())

    body = message.get_content()
    assert message["To"] == "1929@gaca.gov.sa"
    assert "Flight number: SV1674" in body
    assert "Booking reference: 8GAKAN" in body
    assert "Airline complaint reference: C_2800039" in body
    assert "no compensation was offered" in body
    assert "Baggage Services > Baggage Delay" in body
    assert str(message["Message-ID"]) == (
        "<flightdeck-gaca-job-123@example.com>")


def test_gaca_email_delivery_records_smtp_acceptance():
    RecordingSMTP.messages.clear()

    delivery = gaca_email.deliver_gaca_email(
        _payload(),
        "job-123",
        _config(),
        smtp_factory=RecordingSMTP,
        imap_factory=EmptySentMailbox,
    )

    assert delivery.accepted is True
    assert delivery.recovered_from_sent is False
    assert delivery.message_id == "<flightdeck-gaca-job-123@example.com>"
    assert len(RecordingSMTP.messages) == 1


def test_waf_email_copy_does_not_count_as_portal_submission(monkeypatch):
    updates = []
    monkeypatch.setattr(
        "flight_bot.config.load_config", lambda: _config())
    monkeypatch.setattr(
        "flight_bot.gaca_email.deliver_gaca_email",
        lambda *_args, **_kwargs: gaca_email.GacaEmailDelivery(
            accepted=True,
            message_id="<flightdeck-gaca-job-123@example.com>",
        ),
    )
    payload = _payload()

    result = portal_automation._maybe_use_gaca_email_fallback(
        "job-123",
        payload,
        portal_automation.PortalResult(
            "needs_attention",
            "The GACA blocked the VPS browser request (WAF/error page).",
        ),
        lambda *args: updates.append(args),
    )

    assert result.status == "needs_attention"
    assert result.retry_safe is True
    assert payload["gaca_submission_channel"] == "official_email"
    assert payload["gaca_email_message_id"] == (
        "<flightdeck-gaca-job-123@example.com>")
    assert payload["gaca_portal_submission_verified"] is False
    assert updates[-1][0] == "email_copy"


def test_browser_launch_email_copy_remains_retryable(monkeypatch):
    monkeypatch.setattr(
        "flight_bot.config.load_config", lambda: _config())
    monkeypatch.setattr(
        "flight_bot.gaca_email.deliver_gaca_email",
        lambda *_args, **_kwargs: gaca_email.GacaEmailDelivery(
            accepted=True,
            message_id="<flightdeck-gaca-job-456@example.com>",
        ),
    )

    result = portal_automation._maybe_use_gaca_email_fallback(
        "job-456",
        _payload(),
        portal_automation.PortalResult(
            "error",
            "BrowserType.launch_persistent_context: "
            "Target page, context or browser has been closed",
        ),
        lambda *_args: None,
    )

    assert result.status == "needs_attention"
    assert result.retry_safe is True
