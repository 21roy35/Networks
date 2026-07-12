"""Generate complaint letters to GACA and to the airline, pre-filled with
everything scraped from the linked emails, and optionally send the airline
complaint by email (SMTP)."""

import smtplib
import urllib.parse
from datetime import date
from email.message import EmailMessage

from .airlines import AIRLINES, GACA
from .compensation import assess, effective


def _line(label: str, value) -> str:
    return f"  {label}: {value}\n" if value not in (None, "", []) else ""


def _flight_facts(flight: dict, user: dict) -> str:
    tickets = ", ".join(flight.get("ticket_numbers") or []) or None
    flights = ", ".join(flight.get("flight_numbers") or []) or None
    route = None
    if effective(flight, "origin") or effective(flight, "destination"):
        route = (f"{flight.get('origin_city') or ''} ({effective(flight, 'origin') or '?'}) -> "
                 f"{flight.get('destination_city') or ''} ({effective(flight, 'destination') or '?'})")
    facts = ""
    facts += _line("Passenger name", effective(flight, "passenger") or user.get("full_name"))
    facts += _line("Contact email", user.get("email"))
    facts += _line("Contact phone", user.get("phone"))
    facts += _line("Airline", flight.get("airline_name") or flight.get("airline_code"))
    facts += _line("Flight number(s)", flights)
    facts += _line("Flight date", flight.get("flight_date"))
    facts += _line("Route", route)
    facts += _line("Booking reference (PNR)", flight.get("pnr"))
    facts += _line("E-ticket number(s)", tickets)
    facts += _line("Cabin class", flight.get("cabin_class"))
    facts += _line("Seat", flight.get("seat"))
    facts += _line("Scheduled departure", flight.get("departure"))
    facts += _line("Scheduled arrival", flight.get("arrival"))
    facts += _line("Actual/estimated arrival",
                   flight.get("overrides", {}).get("actual_arrival")
                   or flight.get("new_arrival"))
    if flight.get("amount"):
        facts += _line("Ticket price paid",
                       f"{flight.get('currency') or ''} {flight['amount']}".strip())
    facts += _line("Payment method", flight.get("payment_method"))
    return facts


def _incident_summary(flight: dict, assessment: dict) -> str:
    if flight.get("cancelled"):
        summary = ("The flight was cancelled by the airline. I was not "
                   "provided adequate prior notice of the cancellation.")
        notice = flight.get("overrides", {}).get("cancellation_notice_days")
        if notice is not None:
            summary = (f"The flight was cancelled by the airline with "
                       f"approximately {notice} days' notice.")
        return summary
    delay = assessment.get("delay_hours")
    if delay is not None and delay > 0:
        return (f"The flight arrived approximately {delay:g} hours later than "
                "the scheduled arrival time shown on my ticket.")
    if flight.get("overrides", {}).get("denied_boarding"):
        return ("I was denied boarding on this flight despite holding a "
                "confirmed reservation and presenting myself on time.")
    return ("I experienced a service failure on this flight as described "
            "below. [Describe what happened.]")


def gaca_complaint(flight: dict, user: dict) -> dict:
    """Complaint addressed to the Saudi civil aviation regulator."""
    assessment = assess(flight)
    airline = flight.get("airline_name") or flight.get("airline_code") or "the airline"
    subject = (f"Passenger complaint – {airline} "
               f"{flight.get('flight_number') or ''} on "
               f"{flight.get('flight_date') or 'unknown date'} – "
               f"PNR {flight.get('pnr') or 'N/A'}")
    body = f"""To: {GACA['name']}
Date: {date.today().isoformat()}

Subject: {subject}

Dear Sir/Madam,

I wish to file a formal complaint against {airline} under the Customer
Protection Regulation issued by the General Authority of Civil Aviation.

Flight details:
{_flight_facts(flight, user)}
Incident:
  {_incident_summary(flight, assessment)}

Prior contact with the airline:
  [State whether you have already complained to the airline and what, if
  anything, they responded. GACA expects the airline to be given a chance
  to resolve the complaint first.]

Claim:
  Based on the above, I believe I am entitled to compensation and/or care
  under the Customer Protection Regulation, and I request that GACA
  investigate this matter and oblige the carrier to provide the remedy
  prescribed by the Regulation.

Assessment basis: {"; ".join(assessment["frameworks"])}.

Attachments: copies of the booking confirmation, e-ticket, boarding pass
and any delay/cancellation notifications (all available in my email).

Yours faithfully,
{user.get('full_name') or effective(flight, 'passenger') or '[Your name]'}
{user.get('email') or ''}
{user.get('phone') or ''}
"""
    return {
        "to": GACA["name"],
        "portal_url": GACA["portal_url"],
        "phone": GACA["phone"],
        "note": GACA["note"],
        "subject": subject,
        "body": body,
        "assessment": assessment,
    }


def airline_complaint(flight: dict, user: dict) -> dict:
    """Complaint addressed to the operating airline's customer relations."""
    assessment = assess(flight)
    code = flight.get("airline_code")
    info = AIRLINES.get(code, {})
    airline = flight.get("airline_name") or code or "Customer Relations"
    to_email = info.get("complaint_email", "")
    subject = (f"Compensation claim – flight {flight.get('flight_number') or ''} "
               f"on {flight.get('flight_date') or 'unknown date'} – "
               f"PNR {flight.get('pnr') or 'N/A'}")
    body = f"""Dear {airline} Customer Relations,

I am writing to claim compensation and/or a remedy for the disruption I
experienced on the following flight:

{_flight_facts(flight, user)}
What happened:
  {_incident_summary(flight, assessment)}

Legal basis:
  {"; ".join(assessment["frameworks"])}.

I therefore request:
  1. Compensation as prescribed by the applicable regulation;
  2. Reimbursement of any expenses caused by the disruption (receipts
     available on request);
  3. A written response within 30 days.

If I do not receive a satisfactory response, I will escalate this
complaint to the General Authority of Civil Aviation (GACA) and/or the
competent national enforcement body.

Yours faithfully,
{user.get('full_name') or effective(flight, 'passenger') or '[Your name]'}
{user.get('email') or ''}
{user.get('phone') or ''}
"""
    mailto = ""
    if to_email:
        mailto = ("mailto:" + to_email
                  + "?subject=" + urllib.parse.quote(subject)
                  + "&body=" + urllib.parse.quote(body))
    return {
        "to": to_email,
        "airline": airline,
        "complaint_url": info.get("complaint_url", ""),
        "subject": subject,
        "body": body,
        "mailto": mailto,
        "assessment": assessment,
    }


def send_email(config: dict, to_addr: str, subject: str, body: str) -> str:
    """Send the complaint via SMTP. Returns a status string."""
    smtp_cfg = config["smtp"]
    if not smtp_cfg["user"] or not smtp_cfg["password"]:
        return ("SMTP credentials not configured — copy the letter and send "
                "it manually, or set smtp settings in config.json.")
    msg = EmailMessage()
    msg["From"] = smtp_cfg["user"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(smtp_cfg["host"], smtp_cfg.get("port", 587)) as server:
        server.starttls()
        server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.send_message(msg)
    return f"Complaint sent to {to_addr}."
