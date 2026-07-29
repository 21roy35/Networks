from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime

import pytest

from flight_bot import db, mail_client, pipeline


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flightbot.db")
    db.init_db()
    return tmp_path


def _raw_message(number: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<incremental-{number}@example>"
    message["From"] = "booking@saudia.com"
    message["Subject"] = f"Flight booking SV10{number}"
    message.set_content(
        f"Booking reference: ABC12{number}\nFlight SV10{number} RUH to JED")
    return message.as_bytes()


def test_missing_message_id_uses_stable_content_hash():
    message = EmailMessage()
    message["From"] = "booking@saudia.com"
    message["Subject"] = "Flight booking"
    message.set_content("Booking reference: ABC123\nFlight SV101 RUH to JED")
    raw_bytes = message.as_bytes()

    first = mail_client.message_to_raw(message, raw_bytes=raw_bytes)
    second = mail_client.message_to_raw(message, raw_bytes=raw_bytes)

    assert first["message_id"] == second["message_id"]
    assert first["message_id"].startswith("<sha256-")


def test_imap_cursor_fetches_only_new_uids(isolated_db, monkeypatch):
    instances = []

    class FakeIMAP:
        def __init__(self, *_args, **_kwargs):
            self.searches = []
            self.fetches = []
            instances.append(self)

        def login(self, *_args):
            return "OK", []

        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", [b"2"]

        def response(self, name):
            return (name, [b"777" if name == "UIDVALIDITY" else b"13"])

        def uid(self, command, *args):
            if command == "SEARCH":
                query = str(args[-1])
                self.searches.append(query)
                return "OK", [b"11 12"]
            uid = int(args[0])
            self.fetches.append(uid)
            return "OK", [(b"RFC822", _raw_message(uid))]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(mail_client.imaplib, "IMAP4_SSL", FakeIMAP)
    config = {"imap": {
        "host": "imap.example", "port": 993,
        "user": "passenger@example.com", "password": "app-password",
        "folders": ["INBOX"], "since_days": 14,
    }}

    first = list(mail_client.fetch_airline_emails(config, log=lambda *_: None))
    second = list(mail_client.fetch_airline_emails(config, log=lambda *_: None))

    assert len(first) == 2
    assert second == []
    assert instances[0].fetches == [11, 12]
    assert instances[1].fetches == []
    assert instances[1].searches == []
    assert db.get_mailbox_cursor("INBOX")["last_uid"] == 12


def test_old_cursor_runs_one_backfill_when_search_registry_changes(
        isolated_db, monkeypatch):
    class FakeIMAP:
        def __init__(self, *_args, **_kwargs):
            self.searches = []
            self.fetches = []

        def login(self, *_args):
            return "OK", []

        def select(self, _folder, readonly=True):
            return "OK", [b"1"]

        def response(self, name):
            return (name, [b"777" if name == "UIDVALIDITY" else b"13"])

        def uid(self, command, *args):
            if command == "SEARCH":
                self.searches.append(str(args[-1]))
                return "OK", [b"12"]
            uid = int(args[0])
            self.fetches.append(uid)
            return "OK", [(b"RFC822", _raw_message(uid))]

        def logout(self):
            return "BYE", []

    instance = FakeIMAP()
    monkeypatch.setattr(
        mail_client.imaplib, "IMAP4_SSL",
        lambda *_args, **_kwargs: instance)
    db.save_mailbox_cursor("INBOX", "777", 12)
    config = {"imap": {
        "host": "imap.example", "port": 993,
        "user": "passenger@example.com", "password": "app-password",
        "folders": ["INBOX"], "since_days": 730,
    }}

    recovered = list(mail_client.fetch_airline_emails(
        config, log=lambda *_: None))
    second = list(mail_client.fetch_airline_emails(
        config, log=lambda *_: None))

    assert len(recovered) == 1
    assert instance.fetches == [12]
    assert any("SINCE" in query for query in instance.searches)
    assert db.get_mailbox_cursor("INBOX")["query_signature"]
    assert second == []


def test_verification_email_uses_exact_gmail_alias(monkeypatch):
    def message(recipient, code, number):
        value = EmailMessage()
        value["Message-ID"] = f"<otp-{number}@gaca.gov.sa>"
        value["From"] = "no-reply@gaca.gov.sa"
        value["To"] = recipient
        value["Date"] = format_datetime(datetime.now().astimezone())
        value["Subject"] = "GACA EServices - OTP Code"
        value.set_content(f"Verification Code: {code}")
        return value.as_bytes()

    messages = {
        21: message("icrackgames101+2760788@gmail.com", "4821", 21),
        22: message("icrackgames101+other@gmail.com", "9999", 22),
    }

    class FakeIMAP:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            return "OK", []

        def select(self, _folder, readonly=True):
            return "OK", [b"2"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"21 22"]
            uid = int(args[0])
            return "OK", [(b"RFC822", messages[uid])]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(mail_client.imaplib, "IMAP4_SSL", FakeIMAP)
    config = {"imap": {
        "host": "imap.example",
        "port": 993,
        "user": "icrackgames101@gmail.com",
        "password": "app-password",
    }}

    result = mail_client.fetch_recent_verification_message(
        config,
        since=datetime.now().astimezone(),
        recipient="icrackgames101+2760788@gmail.com",
    )

    assert result["message_id"] == "<otp-21@gaca.gov.sa>"
    assert "4821" in result["body"]


def test_empty_incremental_scan_does_not_rebuild_flights(
        isolated_db, monkeypatch):
    rebuilt = []
    monkeypatch.setattr(
        pipeline, "fetch_airline_emails", lambda *_args, **_kwargs: iter(()))
    monkeypatch.setattr(
        pipeline, "rebuild_flights", lambda **_kwargs: rebuilt.append(True))
    progress = {}

    count = pipeline.scan_mailbox(
        {"imap": {}}, log=lambda *_args, **_kwargs: None, progress=progress)

    assert count == 0
    assert rebuilt == []
    assert progress["phase"] == "done"


def test_reset_clears_incremental_and_ai_state(isolated_db):
    db.save_mailbox_cursor("INBOX", "777", 12)
    db.save_ai_analysis_cache(
        "response:test", "airline_response", "model", {"substantive": False})

    db.reset()

    assert db.get_mailbox_cursor("INBOX") is None
    assert db.get_ai_analysis_cache("response:test", "model") is None
