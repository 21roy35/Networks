from datetime import datetime
from email.message import EmailMessage

from flight_bot import db, mail_client
from flight_bot.parser import parse_email


def _ticket(message_id: str, alfursan: str, national_id: str = ""):
    identity = f" National ID: {national_id}" if national_id else ""
    return parse_email(
        message_id,
        "Your Saudia e-ticket SV1650",
        "noreply@saudia.com",
        datetime(2026, 7, 15, 12, 0),
        """Booking reference: ABC123
        Flight SV1650 - JED to AHB
        Passengers Details
        Mr Muhannad Alqahtani e-Ticket: 065-2200741431
        Frequent Flyer: %s%s
        Ms Lujain Alasais e-Ticket: 065-2200741432
        Frequent Flyer: 99887766 National ID: 2233445566
        """ % (alfursan, identity),
    )


def test_identity_fields_are_scoped_to_each_passenger_block():
    parsed = _ticket("<identity-one@example>", "30681234", "1122334455")

    muhannad = parsed.passenger_profiles["muhannad alqahtani"]
    lujain = parsed.passenger_profiles["lujain alasais"]
    assert muhannad["title"] == "Mr"
    assert muhannad["alfursan_id"] == "30681234"
    assert muhannad["national_id"] == "1122334455"
    assert lujain["title"] == "Ms"
    assert lujain["alfursan_id"] == "99887766"
    assert lujain["national_id"] == "2233445566"


def test_conflicting_sensitive_ticket_values_are_not_suggested(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity.db")
    db.init_db()
    db.save_email(_ticket("<identity-one@example>", "30681234"))
    db.save_email(_ticket("<identity-two@example>", "30689999"))

    suggestions = db.identity_suggestions("Muhannad Alqahtani")
    assert "alfursan_id" not in suggestions["values"]
    assert suggestions["conflicts"] == ["alfursan_id"]
    assert suggestions["values"]["title"] == "Mr"


def test_passenger_ai_evidence_and_cache_are_scoped_and_invalidated(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ai-evidence.db")
    db.init_db()
    db.save_email(_ticket("<identity-ai@example>", "30681234"))

    evidence = db.identity_evidence("Muhannad Alqahtani")
    assert evidence
    assert "Muhannad Alqahtani" in evidence[0]["text"]
    assert "Lujain Alasais" not in evidence[0]["text"]

    db.save_ai_profile_cache(
        "Muhannad Alqahtani", "hash-one", "claude-test",
        {"values": {"nationality": "Saudi"}, "evidence": {}})
    assert db.get_ai_profile_cache(
        "Muhannad Alqahtani", "hash-one", "claude-test")["values"] == {
            "nationality": "Saudi"}
    assert db.get_ai_profile_cache(
        "Muhannad Alqahtani", "changed-hash", "claude-test") is None


def test_pdf_attachment_text_is_added_to_the_parseable_email(monkeypatch):
    class Page:
        def extract_text(self):
            return ("Mr Muhannad Alqahtani\n"
                    "Frequent Flyer: 30681234\nNational ID: 1122334455")

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    monkeypatch.setattr(mail_client, "PdfReader", Reader)
    message = EmailMessage()
    message["Message-ID"] = "<pdf-ticket@example>"
    message["From"] = "noreply@saudia.com"
    message["Subject"] = "Your e-ticket"
    message.set_content("Booking reference: ABC123\nFlight SV1650 JED to AHB")
    message.add_attachment(
        b"fake-pdf", maintype="application", subtype="pdf",
        filename="ticket.pdf")

    raw = mail_client.message_to_raw(message)
    assert "[Attachment: ticket.pdf]" in raw["body"]
    parsed = parse_email(raw["message_id"], raw["subject"], raw["sender"],
                         raw["date"], raw["body"])
    profile = parsed.passenger_profiles["muhannad alqahtani"]
    assert profile["alfursan_id"] == "30681234"
    assert profile["national_id"] == "1122334455"
    assert profile["evidence"]["alfursan_id"] == "PDF attachment: ticket.pdf"
