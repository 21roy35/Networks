"""Durable official-email fallback for GACA portal outages."""

from __future__ import annotations

import imaplib
import mimetypes
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path


@dataclass(frozen=True)
class GacaEmailDelivery:
    accepted: bool
    message_id: str = ""
    recovered_from_sent: bool = False
    error: str = ""


def _clean_header(value: object) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _message_id(job_id: str, sender: str) -> str:
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "flightdeck.local"
    safe_job = re.sub(r"[^a-zA-Z0-9.-]", "", str(job_id or ""))
    return f"<flightdeck-gaca-{safe_job}@{domain}>"


def _attachment_path(value: object) -> Path | None:
    if isinstance(value, dict):
        value = value.get("path") or value.get("file_path")
    if not isinstance(value, (str, Path)):
        return None
    path = Path(value)
    try:
        if not path.is_file() or path.stat().st_size > 20_000_000:
            return None
    except OSError:
        return None
    return path


def compose_gaca_email(payload: dict, job_id: str, config: dict) -> EmailMessage:
    """Build a complete escalation without duplicating fields in prose."""
    sender = _clean_header(
        (config.get("imap") or {}).get("user")
        or payload.get("email")
    )
    settings = config.get("gaca_email") or {}
    recipient = _clean_header(
        settings.get("recipient") or "1929@gaca.gov.sa"
    )
    if not sender or not recipient:
        raise ValueError("GACA email sender and recipient are required.")

    airline = _clean_header(
        payload.get("airline_name")
        or payload.get("airline_code")
        or "Airline"
    )
    flight_number = _clean_header(payload.get("flight_number"))
    airline_reference = _clean_header(payload.get("airline_reference"))
    subject = (
        f"GACA airline complaint escalation - {airline}"
        + (f" {flight_number}" if flight_number else "")
        + (f" - {airline_reference}" if airline_reference else "")
    )
    description = str(
        payload.get("description")
        or payload.get("incident")
        or ""
    ).strip()
    category = (
        payload.get("gaca_category")
        or payload.get("portal_category")
        or payload.get("selected_complaint_category")
    )
    if isinstance(category, dict):
        category = " > ".join(
            str(category.get(key) or "").strip()
            for key in ("main", "sub", "detail")
            if str(category.get(key) or "").strip()
        )
    elif isinstance(category, (list, tuple)):
        category = " > ".join(str(item).strip() for item in category if item)
    category = str(category or "").strip()

    rows = [
        ("Passenger name", payload.get("passenger_name")),
        ("National ID / Iqama", payload.get("national_id")),
        ("Contact phone", payload.get("phone")),
        ("Contact email", payload.get("email") or sender),
        ("Airline", airline),
        ("Flight number", flight_number),
        ("Flight date", payload.get("flight_date")),
        ("Route", payload.get("route")),
        ("Booking reference", payload.get("pnr")),
        ("Ticket number", payload.get("ticket_number")),
        ("Airline complaint reference", airline_reference),
        ("Airline complaint date", payload.get("airline_complaint_date")),
        ("Complaint category", category),
    ]
    structured = "\n".join(
        f"{label}: {str(value).strip()}"
        for label, value in rows
        if value not in (None, "")
    )
    body = (
        "Dear GACA Passenger Rights Team,\n\n"
        "I am escalating my unresolved complaint against the airline through "
        "GACA's official complaint email channel because the online complaint "
        "platform is currently returning an access/WAF error.\n\n"
        f"{structured}\n\n"
        "Complaint details:\n"
        f"{description}\n\n"
        "Please register this escalation, confirm receipt, and send me the "
        "GACA complaint reference number.\n\n"
        "Thank you,\n"
        f"{_clean_header(payload.get('passenger_name'))}"
    )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = datetime.now(timezone.utc)
    message["Message-ID"] = _message_id(job_id, sender)
    message["X-FlightDeck-Job"] = _clean_header(job_id)
    message.set_content(body)

    for value in payload.get("attachments") or []:
        path = _attachment_path(value)
        if not path:
            continue
        content_type, _encoding = mimetypes.guess_type(path.name)
        main, sub = (
            content_type.split("/", 1)
            if content_type and "/" in content_type
            else ("application", "octet-stream")
        )
        message.add_attachment(
            path.read_bytes(),
            maintype=main,
            subtype=sub,
            filename=path.name,
        )
    return message


def _already_in_sent(
    config: dict,
    message_id: str,
    *,
    imap_factory=imaplib.IMAP4_SSL,
) -> bool:
    settings = config.get("imap") or {}
    host = str(settings.get("host") or "imap.gmail.com")
    port = int(settings.get("port") or 993)
    user = str(settings.get("user") or "")
    password = str(settings.get("password") or "")
    if not user or not password:
        return False
    client = None
    try:
        client = imap_factory(host, port)
        client.login(user, password)
        for folder in (
            '"[Gmail]/Sent Mail"',
            '"[Google Mail]/Sent Mail"',
            "Sent",
            "Sent Items",
        ):
            status, _data = client.select(folder, readonly=True)
            if status != "OK":
                continue
            status, matches = client.search(
                None, "HEADER", "Message-ID", f'"{message_id}"'
            )
            if status == "OK" and matches and matches[0].split():
                return True
    except (imaplib.IMAP4.error, OSError):
        return False
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return False


def deliver_gaca_email(
    payload: dict,
    job_id: str,
    config: dict,
    *,
    smtp_factory=smtplib.SMTP_SSL,
    imap_factory=imaplib.IMAP4_SSL,
) -> GacaEmailDelivery:
    """Send once, or recover a previous accepted send from Gmail Sent."""
    message = compose_gaca_email(payload, job_id, config)
    message_id = str(message["Message-ID"])
    if _already_in_sent(
        config, message_id, imap_factory=imap_factory
    ):
        return GacaEmailDelivery(
            accepted=True,
            message_id=message_id,
            recovered_from_sent=True,
        )

    imap = config.get("imap") or {}
    settings = config.get("gaca_email") or {}
    user = str(imap.get("user") or "")
    password = str(imap.get("password") or "")
    if not user or not password:
        return GacaEmailDelivery(
            accepted=False,
            message_id=message_id,
            error="Gmail credentials are unavailable.",
        )
    try:
        context = ssl.create_default_context()
        with smtp_factory(
            str(settings.get("smtp_host") or "smtp.gmail.com"),
            int(settings.get("smtp_port") or 465),
            timeout=45,
            context=context,
        ) as smtp:
            smtp.login(user, password)
            refused = smtp.send_message(message)
        if refused:
            return GacaEmailDelivery(
                accepted=False,
                message_id=message_id,
                error="The GACA recipient was refused by the mail server.",
            )
    except (smtplib.SMTPException, OSError) as exc:
        return GacaEmailDelivery(
            accepted=False,
            message_id=message_id,
            error=f"{type(exc).__name__}: official email delivery failed",
        )
    return GacaEmailDelivery(accepted=True, message_id=message_id)
