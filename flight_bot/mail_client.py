"""Fetch airline emails from a mailbox over IMAP.

Works with Gmail (use an App Password: Google Account -> Security ->
2-Step Verification -> App passwords) and any other IMAP provider.
"""

import email
import email.policy
import hashlib
import imaplib
import io
import re
import time
from datetime import datetime, timedelta
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from pypdf import PdfReader

from . import db
from .airlines import all_domains

_SUBJECT_KEYWORDS = [
    "flight", "boarding pass", "e-ticket", "eticket", "itinerary",
    "booking confirmation", "check-in", "your trip", "complaint", "claim",
    "case", "customer relations", "feedback", "reference",
]

_SKIP_TAGS = {"script", "style", "head", "title"}
_VOID_SKIP_TAGS = {"meta", "link"}
_BREAK_TAGS = {"br", "p", "div", "tr", "li", "table", "ul", "ol",
               "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Tolerant HTML -> text: drops script/style/head content entirely."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_SKIP_TAGS:
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_HTML_MARKER_RE = re.compile(
    r"<\s*(?:!doctype|html|head|body|table|div|style|span|td)\b", re.IGNORECASE)
# CSS rules that survive naive tag stripping ("td { padding: 0 }" etc.).
_CSS_RULE_RE = re.compile(r"[^{}\n]{0,200}\{[^{}]*\}")


def looks_like_html(text: str) -> bool:
    return bool(_HTML_MARKER_RE.search(text or ""))


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        pass
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def clean_email_body(body: str) -> str:
    """Normalise a stored/extracted body into plain text.

    Handles HTML that was mislabelled as text/plain and CSS residue left
    behind by earlier, less robust versions of this scraper.
    """
    if not body:
        return ""
    if looks_like_html(body):
        body = html_to_text(body)
    else:
        body = unescape(body)
    for _ in range(4):  # peel nested @media { rule { ... } } blocks
        cleaned = _CSS_RULE_RE.sub(" ", body)
        if cleaned == body:
            break
        body = cleaned
    body = re.sub(r"[ \t\xa0]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n", body).strip()


def extract_body(msg: email.message.EmailMessage) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            content = plain.get_content()
            # Some airlines (e.g. flyadeal) put raw HTML in the plain part.
            return clean_email_body(content)
        except Exception:
            pass
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        try:
            return html_to_text(html.get_content())
        except Exception:
            pass
    return ""


def extract_pdf_attachments(msg: email.message.EmailMessage) -> str:
    """Extract searchable text from reasonably sized ticket PDFs.

    The original attachment is never persisted.  A filename marker is kept
    with the text so profile suggestions can explain where evidence came
    from.  Limits protect the mailbox monitor from unusually large files.
    """
    extracted: list[str] = []
    for part in msg.iter_attachments():
        filename = str(part.get_filename() or "attachment.pdf")
        content_type = (part.get_content_type() or "").lower()
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        try:
            data = part.get_payload(decode=True) or b""
            if not data or len(data) > 15 * 1024 * 1024:
                continue
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages[:25]:
                value = page.extract_text() or ""
                if value:
                    pages.append(value)
                if sum(len(item) for item in pages) >= 150_000:
                    break
            text = clean_email_body("\n".join(pages))[:150_000]
        except Exception:
            continue
        if text:
            safe_name = re.sub(r"[\r\n\[\]]+", " ", filename).strip()
            extracted.append(f"[Attachment: {safe_name}]\n{text}")
    return "\n\n".join(extracted)


def message_to_raw(msg: email.message.EmailMessage,
                   raw_bytes: bytes | None = None) -> dict:
    """Normalise an EmailMessage into the dict the parser consumes."""
    sender = parseaddr(msg.get("From", ""))[1]
    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            date = None
    body = extract_body(msg)
    attachment_text = extract_pdf_attachments(msg)
    if attachment_text:
        body = f"{body}\n\n{attachment_text}".strip()
    if raw_bytes is None:
        raw_bytes = msg.as_bytes(policy=email.policy.default)
    fallback_id = hashlib.sha256(raw_bytes).hexdigest()
    return {
        "message_id": (msg.get("Message-ID")
                       or f"<sha256-{fallback_id}@flightdeck.local>"),
        "subject": str(msg.get("Subject", "")),
        "sender": sender,
        "date": date,
        "body": body,
    }


def fetch_recent_verification_message(
        config: dict,
        *,
        since: datetime | None = None,
        recipient: str = "",
        limit: int = 20) -> dict | None:
    """Return the newest recent OTP email for the active portal recipient."""
    imap_cfg = config.get("imap") or {}
    if not imap_cfg.get("user") or not imap_cfg.get("password"):
        return None
    since = since or datetime.now().astimezone()
    search_since = (since - timedelta(seconds=30)).strftime("%d-%b-%Y")
    conn = imaplib.IMAP4_SSL(
        imap_cfg.get("host") or "imap.gmail.com",
        int(imap_cfg.get("port") or 993))
    try:
        conn.login(imap_cfg["user"], imap_cfg["password"])
        status, _ = conn.select("INBOX", readonly=True)
        if status != "OK":
            return None
        uids: set[bytes] = set()
        for query in (
                f'(SINCE {search_since} SUBJECT "OTP")',
                f'(SINCE {search_since} SUBJECT "verification")',
                f'(SINCE {search_since} FROM "gaca.gov.sa")'):
            try:
                status, data = conn.uid("SEARCH", None, query)
            except imaplib.IMAP4.error:
                continue
            if status == "OK" and data and data[0]:
                uids.update(data[0].split())
        newest = sorted(
            uids, key=lambda value: int(value), reverse=True)[
                :max(1, min(int(limit), 50))]
        for uid in newest:
            status, data = conn.uid("FETCH", uid, "(RFC822)")
            if (status != "OK" or not data or not isinstance(data[0], tuple)
                    or len(data[0]) < 2):
                continue
            raw_bytes = data[0][1]
            msg = email.message_from_bytes(
                raw_bytes, policy=email.policy.default)
            if recipient:
                recipients = getaddresses([
                    *msg.get_all("to", []),
                    *msg.get_all("cc", []),
                    *msg.get_all("delivered-to", []),
                    *msg.get_all("x-original-to", []),
                ])
                expected = recipient.strip().casefold()
                if expected not in {
                        address.strip().casefold()
                        for _name, address in recipients if address}:
                    continue
            raw = message_to_raw(msg, raw_bytes=raw_bytes)
            sent_at = raw.get("date")
            if isinstance(sent_at, datetime):
                threshold = since - timedelta(seconds=30)
                if sent_at.tzinfo is None and threshold.tzinfo is not None:
                    threshold = threshold.replace(tzinfo=None)
                elif sent_at.tzinfo is not None and threshold.tzinfo is None:
                    threshold = threshold.replace(tzinfo=sent_at.tzinfo)
                if sent_at < threshold:
                    continue
            context = " ".join((
                str(raw.get("subject") or ""),
                str(raw.get("sender") or ""),
                str(raw.get("body") or ""),
            ))
            if re.search(
                    r"otp|verification|one[ -]?time|passcode|security code|"
                    r"رمز\s*(?:التحقق|التأكيد|الدخول)",
                    context, re.I):
                return raw
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _search_queries(since: str, first_uid: int | None = None) -> list[str]:
    """IMAP SEARCH criteria: airline sender domains + subject keywords."""
    scope = f"UID {int(first_uid)}:*" if first_uid else f"SINCE {since}"
    queries = [f'({scope} FROM "{domain}")' for domain in all_domains()]
    queries += [f'({scope} SUBJECT "{kw}")' for kw in _SUBJECT_KEYWORDS]
    return queries


def _search_signature() -> str:
    """Version a mailbox cursor against the candidate-search registry.

    When support for a new airline/domain or subject family is deployed, one
    bounded full-window scan is required to recover older messages that the
    previous query set could never see. Subsequent scans remain incremental.
    """
    value = "\n".join(
        sorted(domain.casefold() for domain in all_domains())
        + sorted(keyword.casefold() for keyword in _SUBJECT_KEYWORDS)
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _selected_mailbox_value(conn, name: str) -> str:
    """Return an integer SELECT response such as UIDVALIDITY or UIDNEXT."""
    try:
        _code, values = conn.response(name)
    except (AttributeError, imaplib.IMAP4.error):
        return ""
    if not values:
        return ""
    value = values[-1]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    match = re.search(r"\d+", str(value))
    return match.group(0) if match else ""


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
    query_signature = _search_signature()

    if progress is None:
        progress = {}
    progress.update(phase="connecting", total=0, processed=0)

    log(f"Connecting to {imap_cfg['host']} as {imap_cfg['user']} ...")
    conn = imaplib.IMAP4_SSL(imap_cfg["host"], imap_cfg.get("port", 993))
    try:
        conn.login(imap_cfg["user"], imap_cfg["password"])

        # Phase 1: search every folder first so the total (and therefore an
        # ETA) is known before fetching starts. Once a folder has a cursor,
        # only UIDs newer than its last successful scan are considered.
        progress["phase"] = "searching"
        batches: list[dict] = []
        for folder in imap_cfg.get("folders", ["INBOX"]):
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                log(f"  ! cannot open folder {folder}, skipping")
                continue
            uidvalidity = _selected_mailbox_value(
                conn, "UIDVALIDITY") or "unknown"
            uidnext_text = _selected_mailbox_value(conn, "UIDNEXT")
            cursor = db.get_mailbox_cursor(folder)
            incremental = bool(
                cursor
                and cursor.get("uidvalidity") == uidvalidity
                and cursor.get("query_signature") == query_signature)
            first_uid = int(cursor["last_uid"]) + 1 if incremental else None
            high_uid = (max(0, int(uidnext_text) - 1)
                        if uidnext_text else int(cursor["last_uid"])
                        if incremental else 0)
            uids: set[bytes] = set()
            search_ok = True
            if not (incremental and uidnext_text
                    and high_uid < int(first_uid or 1)):
                for query in _search_queries(since, first_uid):
                    try:
                        status, data = conn.uid("SEARCH", None, query)
                    except imaplib.IMAP4.error:
                        search_ok = False
                        continue
                    if status != "OK":
                        search_ok = False
                        continue
                    if data and data[0]:
                        uids.update(data[0].split())
            if uids:
                high_uid = max(high_uid, max(int(uid) for uid in uids))
            scope = (f"UID {first_uid}+" if incremental
                     else f"since {since}")
            log(f"  {folder}: {len(uids)} new candidate emails ({scope})")
            batches.append({
                "folder": folder,
                "uidvalidity": uidvalidity,
                "high_uid": high_uid,
                "uids": sorted(uids, key=lambda value: int(value)),
                "search_ok": search_ok,
            })

        # Phase 2: fetch, updating progress as we go.
        total = sum(len(batch["uids"]) for batch in batches)
        progress.update(phase="fetching", total=total,
                        fetch_started=time.time())
        for batch in batches:
            folder = batch["folder"]
            status, _ = conn.select(folder, readonly=True)
            folder_ok = batch["search_ok"] and status == "OK"
            for uid in batch["uids"]:
                status, data = conn.uid("FETCH", uid, "(RFC822)")
                progress["processed"] += 1
                if progress["processed"] % 25 == 0:
                    log(f"  fetched {progress['processed']}/{progress['total']} "
                        f"emails (ETA {eta_text(progress) or '...'})")
                if (status != "OK" or not data or data[0] is None
                        or not isinstance(data[0], tuple)
                        or len(data[0]) < 2):
                    folder_ok = False
                    continue
                raw_bytes = data[0][1]
                msg = email.message_from_bytes(
                    raw_bytes, policy=email.policy.default)
                yield message_to_raw(msg, raw_bytes=raw_bytes)
            if folder_ok:
                db.save_mailbox_cursor(
                    folder, batch["uidvalidity"], batch["high_uid"],
                    query_signature)
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
