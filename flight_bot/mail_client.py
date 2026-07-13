"""Fetch airline emails from a mailbox over IMAP.

Works with Gmail (use an App Password: Google Account -> Security ->
2-Step Verification -> App passwords) and any other IMAP provider.
"""

import email
import email.policy
import imaplib
import re
import time
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


def fetch_airline_emails(config: dict, log=print, progress: dict | None = None):
    """Yield raw email dicts for every candidate airline email found.

    `progress` (if given) is updated in place with phase / total /
    processed / fetch_started so callers can display a live ETA.
    """
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

    if progress is None:
        progress = {}
    progress.update(phase="connecting", total=0, processed=0)

    log(f"Connecting to {imap_cfg['host']} as {imap_cfg['user']} ...")
    conn = imaplib.IMAP4_SSL(imap_cfg["host"], imap_cfg.get("port", 993))
    try:
        conn.login(imap_cfg["user"], imap_cfg["password"])

        # Phase 1: search every folder first so the total (and therefore an
        # ETA) is known before fetching starts.
        progress["phase"] = "searching"
        todo: list[tuple[str, bytes]] = []
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
            todo.extend((folder, uid) for uid in
                        sorted(uids, key=lambda u: int(u)))

        # Phase 2: fetch, updating progress as we go.
        progress.update(phase="fetching", total=len(todo),
                        fetch_started=time.time())
        current_folder = None
        for folder, uid in todo:
            if folder != current_folder:
                conn.select(folder, readonly=True)
                current_folder = folder
            status, data = conn.uid("FETCH", uid, "(RFC822)")
            progress["processed"] += 1
            if progress["processed"] % 25 == 0:
                log(f"  fetched {progress['processed']}/{progress['total']} "
                    f"emails (ETA {eta_text(progress) or '...'})")
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


def eta_text(progress: dict) -> str | None:
    """Human-readable time remaining for the fetch phase, e.g. '1m 24s'."""
    total = progress.get("total") or 0
    processed = progress.get("processed") or 0
    started = progress.get("fetch_started")
    if not started or not total or processed < 3 or processed >= total:
        return None
    rate = (time.time() - started) / processed
    remaining = int(rate * (total - processed))
    if remaining >= 60:
        return f"{remaining // 60}m {remaining % 60:02d}s"
    return f"{remaining}s"


def load_eml_files(directory, log=print):
    """Yield raw email dicts from .eml files (demo mode / exports)."""
    from pathlib import Path
    files = sorted(Path(directory).glob("*.eml"))
    log(f"Loading {len(files)} .eml files from {directory}")
    for path in files:
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
        yield message_to_raw(msg)
