"""Fetch airline emails from a mailbox over IMAP.

Works with Gmail (use an App Password: Google Account -> Security ->
2-Step Verification -> App passwords) and any other IMAP provider.
"""

import email
import email.policy
import imaplib
import re
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime

from .airlines import all_domains

_SUBJECT_KEYWORDS = [
    "flight", "boarding pass", "e-ticket", "eticket", "itinerary",
    "booking confirmation", "check-in", "your trip",
]

_HTML_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS_RE = re.compile(r"<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", html)
    text = _TAGS_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_body(msg: email.message.EmailMessage) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            return plain.get_content()
        except Exception:
            pass
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        try:
            return html_to_text(html.get_content())
        except Exception:
            pass
    return ""


def message_to_raw(msg: email.message.EmailMessage) -> dict:
    """Normalise an EmailMessage into the dict the parser consumes."""
    sender = parseaddr(msg.get("From", ""))[1]
    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            date = None
    return {
        "message_id": msg.get("Message-ID") or f"<no-id-{hash(str(msg))}>",
        "subject": str(msg.get("Subject", "")),
        "sender": sender,
        "date": date,
        "body": extract_body(msg),
    }


def _search_queries(since: str) -> list[str]:
    """IMAP SEARCH criteria: airline sender domains + subject keywords."""
    queries = [f'(SINCE {since} FROM "{domain}")' for domain in all_domains()]
    queries += [f'(SINCE {since} SUBJECT "{kw}")' for kw in _SUBJECT_KEYWORDS]
    return queries


def fetch_airline_emails(config: dict, log=print):
    """Yield raw email dicts for every candidate airline email found."""
    imap_cfg = config["imap"]
    if not imap_cfg["user"] or not imap_cfg["password"]:
        raise SystemExit(
            "IMAP credentials missing. Set them in config.json or via "
            "FLIGHTBOT_IMAP_USER / FLIGHTBOT_IMAP_PASSWORD environment "
            "variables. For Gmail, create an App Password "
            "(https://myaccount.google.com/apppasswords)."
        )

    since_dt = datetime.now() - timedelta(days=imap_cfg.get("since_days", 730))
    since = since_dt.strftime("%d-%b-%Y")

    log(f"Connecting to {imap_cfg['host']} as {imap_cfg['user']} ...")
    conn = imaplib.IMAP4_SSL(imap_cfg["host"], imap_cfg.get("port", 993))
    try:
        conn.login(imap_cfg["user"], imap_cfg["password"])
        for folder in imap_cfg.get("folders", ["INBOX"]):
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                log(f"  ! cannot open folder {folder}, skipping")
                continue
            uids: set[bytes] = set()
            for query in _search_queries(since):
                try:
                    status, data = conn.uid("SEARCH", None, query)
                except imaplib.IMAP4.error:
                    continue
                if status == "OK" and data and data[0]:
                    uids.update(data[0].split())
            log(f"  {folder}: {len(uids)} candidate emails since {since}")
            for uid in sorted(uids, key=lambda u: int(u)):
                status, data = conn.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not data or data[0] is None:
                    continue
                msg = email.message_from_bytes(
                    data[0][1], policy=email.policy.default)
                yield message_to_raw(msg)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def load_eml_files(directory, log=print):
    """Yield raw email dicts from .eml files (demo mode / exports)."""
    from pathlib import Path
    files = sorted(Path(directory).glob("*.eml"))
    log(f"Loading {len(files)} .eml files from {directory}")
    for path in files:
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
        yield message_to_raw(msg)
