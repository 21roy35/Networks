"""SQLite storage for parsed emails, linked flights and manual overrides."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import DB_PATH, passenger_profile_key


_ACTIVE_PORTAL_JOB_STATUSES = (
    "queued", "retry_wait", "leased", "opening", "filling", "reviewing",
    "verification", "submitting",
)


def gaca_identity_key(payload: dict | None) -> str:
    """Return a non-PII key for the passenger used on GACA Step 2."""
    payload = payload if isinstance(payload, dict) else {}
    national_id = re.sub(
        r"[^0-9A-Za-z]", "",
        str(
            payload.get("national_id")
            or payload.get("passport_number")
            or payload.get("passport")
            or payload.get("iqama")
            or ""
        ),
    ).casefold()
    if national_id:
        identity = f"id:{national_id}"
    else:
        passenger_name = str(
            payload.get("profile_passenger_name")
            or payload.get("passenger_name")
            or " ".join(filter(None, (
                payload.get("first_name"),
                payload.get("middle_name"),
                payload.get("last_name"),
            )))
            or ""
        )
        name_key = passenger_profile_key(passenger_name)
        phone = re.sub(r"\D", "", str(payload.get("phone") or ""))
        email = str(payload.get("email") or "").strip().casefold()
        if not any((name_key, phone, email)):
            return ""
        # Include the name even when relatives share a phone number or email.
        identity = f"name:{name_key}|phone:{phone}|email:{email}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender TEXT,
    date TEXT,
    kinds TEXT,          -- JSON list
    parsed TEXT,         -- JSON of all extracted fields
    body TEXT
);

CREATE TABLE IF NOT EXISTS mail_events (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender TEXT,
    date TEXT,
    body TEXT
);

CREATE TABLE IF NOT EXISTS sms_messages (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    sender TEXT,
    received_at TEXT,
    body TEXT NOT NULL,
    source TEXT,
    mail_event_id INTEGER REFERENCES mail_events(id),
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS otp_receipts (
    fingerprint TEXT PRIMARY KEY,
    first_seen_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mailbox_cursors (
    folder TEXT PRIMARY KEY,
    uidvalidity TEXT NOT NULL,
    last_uid INTEGER NOT NULL,
    query_signature TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY,
    flight_key TEXT UNIQUE NOT NULL,
    data TEXT NOT NULL,      -- JSON merged flight record
    overrides TEXT DEFAULT '{}'  -- JSON of user-entered corrections
);

CREATE TABLE IF NOT EXISTS flight_emails (
    flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    UNIQUE (flight_id, email_id)
);

CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY,
    flight_key TEXT NOT NULL,    -- survives re-linking (flight ids change)
    kind TEXT NOT NULL,          -- 'airline' | 'gaca'
    parent_complaint_id INTEGER REFERENCES complaints(id),
    root_complaint_id INTEGER REFERENCES complaints(id),
    generation INTEGER NOT NULL DEFAULT 0,
    to_addr TEXT,
    subject TEXT,
    reference TEXT,
    details TEXT,
    original_text TEXT,
    submitted_text TEXT,
    portal_category TEXT,
    issue_summary TEXT,
    requested_resolution_summary TEXT,
    provider_response_text TEXT,
    response_summary TEXT,
    submission_source TEXT NOT NULL DEFAULT 'automation',
    escalate_parent_on_success INTEGER NOT NULL DEFAULT 0,
    attachments TEXT DEFAULT '[]',
    status TEXT NOT NULL,        -- 'sent' | 'filed'
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS complaint_responses (
    complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    mail_event_id INTEGER NOT NULL UNIQUE
        REFERENCES mail_events(id) ON DELETE CASCADE,
    match_method TEXT NOT NULL,  -- exact_reference | case_facts
    matched_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (complaint_id, mail_event_id)
);

CREATE TABLE IF NOT EXISTS complaint_response_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_surveys (
    flight_key TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    prompt_message_id INTEGER,
    status TEXT NOT NULL,
    asked_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS telegram_events (
    event_key TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS telegram_messages (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,       -- incoming | outgoing
    message_id INTEGER,
    text TEXT,
    media_kind TEXT,
    reply_to_message_id INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(direction, message_id)
);

CREATE TABLE IF NOT EXISTS telegram_ticket_imports (
    token TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    source_message_id INTEGER,
    source_kind TEXT NOT NULL,
    source_file TEXT,
    source_text TEXT,
    extracted TEXT NOT NULL DEFAULT '{}',
    imported_flight_keys TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(chat_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS portal_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT,
    identity_key TEXT,
    airline_code TEXT,
    flight_number TEXT,
    flight_key TEXT,
    complaint_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    message TEXT,
    reference TEXT,
    screenshot_file TEXT,
    terminal INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 100000,
    next_attempt_at REAL,
    lease_until REAL,
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (complaint_id) REFERENCES complaints(id)
);

CREATE TABLE IF NOT EXISTS gaca_account_cases (
    case_key TEXT PRIMARY KEY,
    reference TEXT,
    status TEXT,
    service_type TEXT,
    airline TEXT,
    flight_number TEXT,
    flight_date TEXT,
    airline_reference TEXT,
    ticket_number TEXT,
    pnr TEXT,
    passenger_name TEXT,
    origin TEXT,
    destination TEXT,
    category TEXT,
    submitted_at TEXT,
    complaint_text TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    mapped_complaint_id INTEGER REFERENCES complaints(id),
    mapped_flight_key TEXT,
    mapping_status TEXT NOT NULL DEFAULT 'unmapped',
    match_method TEXT,
    match_score INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT DEFAULT (datetime('now', 'localtime')),
    last_seen_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS gaca_account_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    message TEXT,
    cases_seen INTEGER NOT NULL DEFAULT 0,
    cases_mapped INTEGER NOT NULL DEFAULT 0,
    cases_reconciled INTEGER NOT NULL DEFAULT 0,
    cases_ambiguous INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_success_at TEXT,
    screenshot_file TEXT
);

CREATE TABLE IF NOT EXISTS gaca_status_checks (
    id INTEGER PRIMARY KEY,
    complaint_id INTEGER NOT NULL UNIQUE
        REFERENCES complaints(id) ON DELETE CASCADE,
    reference TEXT NOT NULL,
    details_url TEXT NOT NULL,
    trigger_sms_id INTEGER REFERENCES sms_messages(id),
    status TEXT NOT NULL DEFAULT 'queued',
    case_status TEXT,
    response_text TEXT,
    response_summary TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_profile_cache (
    passenger_key TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS ai_analysis_cache (
    cache_key TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS flight_status_observations (
    id INTEGER PRIMARY KEY,
    flight_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_flight_id TEXT,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    observed_at TEXT NOT NULL,
    source_timestamp TEXT,
    raw_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (flight_key, provider, raw_hash)
);

CREATE TABLE IF NOT EXISTS flight_status_current (
    flight_key TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_events_date
    ON mail_events(date, id);
CREATE INDEX IF NOT EXISTS idx_sms_messages_date
    ON sms_messages(received_at, id);
CREATE INDEX IF NOT EXISTS idx_emails_date
    ON emails(date, id);
CREATE INDEX IF NOT EXISTS idx_complaints_open
    ON complaints(kind, status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_complaint_responses_complaint
    ON complaint_responses(complaint_id, matched_at);
CREATE INDEX IF NOT EXISTS idx_telegram_surveys_status
    ON telegram_surveys(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_created
    ON telegram_messages(created_at, id);
CREATE INDEX IF NOT EXISTS idx_telegram_ticket_imports_status
    ON telegram_ticket_imports(chat_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_portal_jobs_updated
    ON portal_jobs(updated_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gaca_account_reference_unique
    ON gaca_account_cases(lower(trim(reference)))
    WHERE reference IS NOT NULL AND length(trim(reference)) > 0;
CREATE INDEX IF NOT EXISTS idx_gaca_account_mapping
    ON gaca_account_cases(mapping_status, mapped_flight_key, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_gaca_status_checks_due
    ON gaca_status_checks(status, next_attempt_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_flight_status_observations_lookup
    ON flight_status_observations(flight_key, observed_at DESC, id DESC);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.executescript(_SCHEMA)
        # Remove legacy/false account rows that lack both an official GACA
        # reference and an original airline complaint reference.
        conn.execute(
            """DELETE FROM gaca_account_cases
               WHERE length(trim(COALESCE(reference, ''))) = 0
                 AND length(trim(COALESCE(airline_reference, ''))) = 0""")
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(complaints)")}
        if "reference" not in columns:
            conn.execute("ALTER TABLE complaints ADD COLUMN reference TEXT")
        if "details" not in columns:
            conn.execute("ALTER TABLE complaints ADD COLUMN details TEXT")
        if "attachments" not in columns:
            conn.execute(
                "ALTER TABLE complaints ADD COLUMN attachments TEXT DEFAULT '[]'")
        if "submitted_text" not in columns:
            conn.execute(
                "ALTER TABLE complaints ADD COLUMN submitted_text TEXT")
        if "portal_category" not in columns:
            conn.execute(
                "ALTER TABLE complaints ADD COLUMN portal_category TEXT")
        complaint_migrations = {
            "parent_complaint_id": "INTEGER REFERENCES complaints(id)",
            "root_complaint_id": "INTEGER REFERENCES complaints(id)",
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "original_text": "TEXT",
            "issue_summary": "TEXT",
            "requested_resolution_summary": "TEXT",
            "provider_response_text": "TEXT",
            "response_summary": "TEXT",
            "submission_source": "TEXT NOT NULL DEFAULT 'automation'",
            "escalate_parent_on_success": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in complaint_migrations.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE complaints ADD COLUMN {name} {declaration}")
        cursor_columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(mailbox_cursors)")
        }
        if "query_signature" not in cursor_columns:
            conn.execute(
                "ALTER TABLE mailbox_cursors "
                "ADD COLUMN query_signature TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """UPDATE complaints
               SET original_text = COALESCE(
                       NULLIF(original_text, ''),
                       NULLIF(details, ''),
                       NULLIF(submitted_text, ''),
                       ''),
                   root_complaint_id = COALESCE(root_complaint_id, id),
                   generation = COALESCE(generation, 0)""")
        # A provider reference identifies one complaint. This database-level
        # guard prevents delayed duplicate SMS/email acknowledgements from
        # ever being copied to a different complaint, including across restarts.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   idx_complaints_kind_reference_unique
               ON complaints(kind, lower(trim(reference)))
               WHERE reference IS NOT NULL
                 AND length(trim(reference)) > 0""")
        portal_columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(portal_jobs)")
        }
        portal_migrations = {
            "complaint_id": "INTEGER REFERENCES complaints(id)",
            "payload": "TEXT NOT NULL DEFAULT '{}'",
            "identity_key": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 100000",
            "next_attempt_at": "REAL",
            "lease_until": "REAL",
            "last_error": "TEXT",
        }
        for name, declaration in portal_migrations.items():
            if name not in portal_columns:
                conn.execute(
                    f"ALTER TABLE portal_jobs ADD COLUMN {name} {declaration}")
        # CREATE INDEX in the base schema runs before migrations, so existing
        # databases need the identity index after the column is added.
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_portal_jobs_identity_due
               ON portal_jobs(kind, identity_key, status, next_attempt_at)""")
        for row in conn.execute(
                """SELECT id, payload FROM portal_jobs
                   WHERE kind='gaca'
                     AND COALESCE(identity_key, '')=''""").fetchall():
            try:
                stored_payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                stored_payload = {}
            identity_key = gaca_identity_key(stored_payload)
            if identity_key:
                conn.execute(
                    "UPDATE portal_jobs SET identity_key=? WHERE id=?",
                    (identity_key, str(row["id"])),
                )
        conn.execute(
            """UPDATE portal_jobs
               SET max_attempts = 100000
               WHERE terminal = 0 AND max_attempts < 100000""")
        # GACA does not leave Submit attempts in a permanent "unknown" or
        # quarantine state.  The latest attempt for each flight becomes the
        # single durable retry; older same-flight attempts are superseded so a
        # restart can never launch duplicate jobs.
        ambiguous_gaca = conn.execute(
            """SELECT id, complaint_id, flight_key
               FROM portal_jobs
               WHERE kind='gaca'
                 AND status IN ('confirmation_unknown', 'quarantined')
               ORDER BY flight_key, created_at DESC, id DESC"""
        ).fetchall()
        canonical_by_flight: dict[str, str] = {}
        retry_at = time.time() + 15 * 60
        for row in ambiguous_gaca:
            flight_key = str(row["flight_key"] or "")
            if flight_key not in canonical_by_flight:
                canonical_by_flight[flight_key] = str(row["id"])
                conn.execute(
                    """UPDATE portal_jobs
                       SET status='retry_wait', terminal=0, lease_until=NULL,
                           next_attempt_at=?, last_error=?,
                           message=?,
                           updated_at=datetime('now', 'localtime')
                       WHERE id=?""",
                    (
                        retry_at,
                        "legacy GACA ambiguity reconciled as not submitted",
                        "No GACA acceptance/reference was verified. Email and "
                        "SMS remain monitored; this canonical job is queued for "
                        "one safe retry.",
                        str(row["id"]),
                    ),
                )
                if row["complaint_id"] is not None:
                    conn.execute(
                        "UPDATE complaints SET status='filing' WHERE id=?",
                        (int(row["complaint_id"]),),
                    )
            else:
                conn.execute(
                    """UPDATE portal_jobs
                       SET status='superseded', terminal=1, lease_until=NULL,
                           next_attempt_at=NULL, last_error=?,
                           message=?,
                           updated_at=datetime('now', 'localtime')
                       WHERE id=?""",
                    (
                        "newer same-flight GACA retry is canonical",
                        "Superseded by the latest durable GACA job for this "
                        "flight; this row was not submitted.",
                        str(row["id"]),
                    ),
                )
                if row["complaint_id"] is not None:
                    conn.execute(
                        "UPDATE complaints SET status='failed' WHERE id=?",
                        (int(row["complaint_id"]),),
                    )
        # Older builds incorrectly treated an unreadable portal confirmation
        # as a protected success. Such rows are failures and must never unlock
        # a GACA escalation or suppress a safe retry.
        conn.execute(
            """UPDATE complaints SET status = 'failed'
               WHERE status = 'confirmation_unknown'
                  OR (kind = 'airline' AND status = 'submitted'
                      AND TRIM(COALESCE(reference, '')) = '')""")


def save_mail_event(raw: dict) -> int:
    """Store every candidate airline message, including non-flight replies."""
    value = raw.get("date")
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    with connect() as conn:
        conn.execute(
            """INSERT INTO mail_events (message_id, subject, sender, date, body)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO UPDATE SET
                 subject=excluded.subject, sender=excluded.sender,
                 date=excluded.date, body=excluded.body""",
            (raw.get("message_id"), raw.get("subject"), raw.get("sender"),
             value, raw.get("body")),
        )
        return conn.execute(
            "SELECT id FROM mail_events WHERE message_id = ?",
            (raw.get("message_id"),)).fetchone()["id"]


def save_sms_message(record: dict) -> tuple[int, bool]:
    """Persist an SMS and report whether this delivery was new."""
    with connect() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO sms_messages
                   (fingerprint, sender, received_at, body, source)
               VALUES (?, ?, ?, ?, ?)""",
            (record["fingerprint"], record.get("sender"),
             record.get("received_at"), record.get("body") or "",
             record.get("source") or "telecombot-shortcut"),
        )
        row = conn.execute(
            "SELECT id FROM sms_messages WHERE fingerprint = ?",
            (record["fingerprint"],)).fetchone()
        return int(row["id"]), cursor.rowcount == 1


def link_sms_mail_event(sms_id: int, mail_event_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sms_messages SET mail_event_id = ? WHERE id = ?",
            (mail_event_id, sms_id))


def list_sms_messages(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sms_messages ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),)).fetchall()
    return [dict(row) for row in rows]


def queue_gaca_status_check(
        reference: str,
        details_url: str,
        *,
        trigger_sms_id: int | None = None,
        urgent: bool = False) -> int | None:
    """Schedule the official SMS Details check for one known GACA case."""
    reference = str(reference or "").strip()
    if not reference or not details_url:
        return None
    due = time.time() if urgent else time.time() + 24 * 60 * 60
    with connect() as conn:
        complaint = conn.execute(
            """SELECT id FROM complaints
               WHERE kind='gaca' AND lower(trim(reference))=lower(trim(?))
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (reference,),
        ).fetchone()
        if not complaint:
            return None
        existing = conn.execute(
            """SELECT id, trigger_sms_id, status, next_attempt_at
               FROM gaca_status_checks WHERE complaint_id=?""",
            (int(complaint["id"]),),
        ).fetchone()
        if existing and trigger_sms_id is not None:
            prior_sms = int(existing["trigger_sms_id"] or 0)
            if int(trigger_sms_id) <= prior_sms:
                return int(existing["id"])
        if existing:
            next_due = due
            if not urgent and existing["next_attempt_at"] is not None:
                next_due = min(float(existing["next_attempt_at"]), due)
            conn.execute(
                """UPDATE gaca_status_checks
                   SET reference=?, details_url=?,
                       trigger_sms_id=COALESCE(?, trigger_sms_id),
                       status='queued', next_attempt_at=?, last_error=NULL,
                       updated_at=datetime('now', 'localtime')
                   WHERE id=?""",
                (
                    reference, details_url, trigger_sms_id,
                    next_due, int(existing["id"]),
                ),
            )
            return int(existing["id"])
        cursor = conn.execute(
            """INSERT INTO gaca_status_checks
                   (complaint_id, reference, details_url, trigger_sms_id,
                    status, next_attempt_at)
               VALUES (?, ?, ?, ?, 'queued', ?)""",
            (
                int(complaint["id"]), reference, details_url,
                trigger_sms_id, due,
            ),
        )
        return int(cursor.lastrowid)


def claim_due_gaca_status_check() -> dict | None:
    """Lease one due GACA status lookup; stale interrupted checks recover."""
    now = time.time()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE gaca_status_checks
               SET status='retry_wait', next_attempt_at=?,
                   last_error='interrupted status check recovered',
                   updated_at=datetime('now', 'localtime')
               WHERE status='checking'
                 AND updated_at < datetime('now', 'localtime', '-20 minutes')""",
            (now,),
        )
        row = conn.execute(
            """SELECT * FROM gaca_status_checks
               WHERE status IN ('queued', 'retry_wait')
                 AND COALESCE(next_attempt_at, 0) <= ?
               ORDER BY COALESCE(next_attempt_at, 0), id
               LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """UPDATE gaca_status_checks
               SET status='checking', attempts=attempts+1,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (int(row["id"]),),
        )
        claimed = conn.execute(
            "SELECT * FROM gaca_status_checks WHERE id=?",
            (int(row["id"]),),
        ).fetchone()
    return dict(claimed) if claimed else None


def has_due_gaca_status_check() -> bool:
    with connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM gaca_status_checks
               WHERE status IN ('queued', 'retry_wait')
                 AND COALESCE(next_attempt_at, 0) <= ?
               LIMIT 1""",
            (time.time(),),
        ).fetchone()
    return bool(row)


def finish_gaca_status_check(
        check_id: int,
        *,
        case_status: str,
        response_text: str,
        response_summary: str = "") -> None:
    """Store the verified regulator outcome and schedule open cases daily."""
    case_status = str(case_status or "unknown")
    terminal = case_status in {"closed", "rejected", "canceled", "solved"}
    with connect() as conn:
        conn.execute(
            """UPDATE gaca_status_checks
               SET status=?, case_status=?, response_text=?,
                   response_summary=?, last_error=NULL,
                   next_attempt_at=?, checked_at=datetime('now', 'localtime'),
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (
                "checked" if terminal else "retry_wait",
                case_status,
                str(response_text or ""),
                str(response_summary or ""),
                None if terminal else time.time() + 24 * 60 * 60,
                int(check_id),
            ),
        )


def retry_gaca_status_check(check_id: int, error: str) -> None:
    """Retry transient checker failures without repeatedly requesting OTPs."""
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts FROM gaca_status_checks WHERE id=?",
            (int(check_id),),
        ).fetchone()
        attempts = max(1, int((row or {"attempts": 1})["attempts"] or 1))
        delay = min(6 * 60 * 60, 15 * 60 * (2 ** min(attempts - 1, 4)))
        conn.execute(
            """UPDATE gaca_status_checks
               SET status='retry_wait', last_error=?, next_attempt_at=?,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (str(error or "")[:1000], time.time() + delay, int(check_id)),
        )


def list_gaca_status_checks(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, c.flight_key
               FROM gaca_status_checks s
               JOIN complaints c ON c.id=s.complaint_id
               ORDER BY s.updated_at DESC, s.id DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def remember_otp_receipt(fingerprint: str) -> bool:
    """Deduplicate OTP deliveries without persisting their code or message body."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM otp_receipts "
            "WHERE first_seen_at < datetime('now', '-10 minutes')")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO otp_receipts(fingerprint) VALUES (?)",
            (fingerprint,))
        return cursor.rowcount == 1


def list_mail_events() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM mail_events ORDER BY date DESC").fetchall()
    return [dict(row) for row in rows]


def get_mailbox_cursor(folder: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM mailbox_cursors WHERE folder = ?",
            (folder,)).fetchone()
    return dict(row) if row else None


def save_mailbox_cursor(folder: str, uidvalidity: str,
                        last_uid: int, query_signature: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO mailbox_cursors
                   (folder, uidvalidity, last_uid, query_signature, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(folder) DO UPDATE SET
                   uidvalidity=excluded.uidvalidity,
                   last_uid=excluded.last_uid,
                   query_signature=excluded.query_signature,
                   updated_at=excluded.updated_at""",
            (folder, str(uidvalidity), int(last_uid),
             str(query_signature or "")))


def mailbox_cursor_summary() -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS folders, MAX(updated_at) AS last_success
               FROM mailbox_cursors""").fetchone()
    return dict(row)


def link_complaint_response(complaint_id: int, mail_event_id: int,
                            match_method: str) -> bool:
    """Persist one resolution email's complaint assignment exactly once."""
    with connect() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO complaint_responses
                   (complaint_id, mail_event_id, match_method)
               VALUES (?, ?, ?)""",
            (complaint_id, mail_event_id, match_method))
        return cursor.rowcount == 1


def unlink_complaint_response(complaint_id: int,
                              mail_event_id: int | None = None) -> int:
    """Remove a false or superseded response link for one complaint."""
    with connect() as conn:
        if mail_event_id is None:
            cursor = conn.execute(
                "DELETE FROM complaint_responses WHERE complaint_id = ?",
                (complaint_id,))
        else:
            cursor = conn.execute(
                """DELETE FROM complaint_responses
                   WHERE complaint_id = ? AND mail_event_id = ?""",
                (complaint_id, mail_event_id))
        return int(cursor.rowcount or 0)


def list_complaint_responses() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM complaint_responses
               ORDER BY matched_at, mail_event_id""").fetchall()
    return [dict(row) for row in rows]


def complaint_response_details(limit: int = 10) -> list[dict]:
    """Return response links with the actual case and email facts attached."""
    limit = max(1, min(int(limit or 10), 50))
    with connect() as conn:
        rows = conn.execute(
            """SELECT cr.complaint_id, cr.mail_event_id, cr.match_method,
                      cr.matched_at, c.reference, c.kind, c.status,
                      c.flight_key, me.subject, me.sender, me.date, me.body
               FROM complaint_responses cr
               JOIN complaints c ON c.id = cr.complaint_id
               JOIN mail_events me ON me.id = cr.mail_event_id
               ORDER BY COALESCE(me.date, cr.matched_at) DESC,
                        cr.mail_event_id DESC
               LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in rows]


def search_mail_events(query: str = "", limit: int = 10) -> list[dict]:
    """Search stored airline messages without loading the whole inbox."""
    limit = max(1, min(int(limit or 10), 50))
    query = " ".join(str(query or "").split()).strip()
    with connect() as conn:
        if query:
            pattern = f"%{query.casefold()}%"
            rows = conn.execute(
                """SELECT * FROM mail_events
                   WHERE LOWER(COALESCE(subject, '')) LIKE ?
                      OR LOWER(COALESCE(sender, '')) LIKE ?
                      OR LOWER(COALESCE(body, '')) LIKE ?
                   ORDER BY date DESC, id DESC LIMIT ?""",
                (pattern, pattern, pattern, limit)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM mail_events
                   ORDER BY date DESC, id DESC LIMIT ?""",
                (limit,)).fetchall()
    return [dict(row) for row in rows]


def initialize_fifo_response_floor() -> int:
    """Snapshot the pre-feature inbox once so old unreferenced mail is ignored."""
    key = "fifo_resolution_event_floor_v1"
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state_value FROM complaint_response_state WHERE state_key = ?",
            (key,)).fetchone()
        if row:
            return int(row["state_value"])
        latest = int(conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM mail_events").fetchone()[0])
        conn.execute(
            "INSERT INTO complaint_response_state (state_key, state_value) "
            "VALUES (?, ?)", (key, str(latest)))
        return latest


_GACA_IDENTITY_CIRCUIT_PREFIX = "gaca_identity_rate_limit_until_v1:"


def gaca_identity_circuit_until(
        payload: dict | None = None, *, identity_key: str = "") -> float:
    """Return the cooldown for one GACA passenger identity."""
    key = str(identity_key or gaca_identity_key(payload)).strip()
    if not key:
        return 0.0
    with connect() as conn:
        row = conn.execute(
            """SELECT state_value FROM complaint_response_state
               WHERE state_key=?""",
            (_GACA_IDENTITY_CIRCUIT_PREFIX + key,),
        ).fetchone()
    try:
        return float(row["state_value"]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def apply_gaca_identity_circuit(job_id: str) -> float:
    """Move a newly queued GACA job behind its passenger's active cooldown."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            """SELECT p.identity_key, s.state_value
               FROM portal_jobs p
               LEFT JOIN complaint_response_state s
                 ON s.state_key=? || p.identity_key
               WHERE p.id=? AND p.kind='gaca'""",
            (_GACA_IDENTITY_CIRCUIT_PREFIX, str(job_id)),
        ).fetchone()
        try:
            deadline = float(row["state_value"]) if row else 0.0
        except (TypeError, ValueError):
            deadline = 0.0
        if deadline <= now:
            return 0.0
        conn.execute(
            """UPDATE portal_jobs
               SET status='retry_wait', terminal=0, lease_until=NULL,
                   next_attempt_at=?, message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE id=? AND status IN ('queued', 'retry_wait')""",
            (
                deadline,
                "This passenger is still inside GACA's 24-hour identity "
                "cooldown. The complaint is saved and will start afterward.",
                str(job_id),
            ),
        )
    return deadline


def defer_gaca_identity_jobs(
        current_job_id: str, error: str, *,
        payload: dict | None = None, identity_key: str = "",
        delay: int = 24 * 3600, spacing: int = 24 * 3600) -> float:
    """Pause only one passenger after GACA rejects their Step 2 identity.

    Other family members remain eligible. Multiple complaints for the same
    passenger are serialized so they cannot all retry when the cooldown ends.
    """
    delay = max(3600, min(int(delay), 7 * 24 * 3600))
    spacing = max(15 * 60, min(int(spacing), 7 * 24 * 3600))
    with connect() as conn:
        row = conn.execute(
            "SELECT identity_key,payload FROM portal_jobs WHERE id=?",
            (str(current_job_id),),
        ).fetchone()
        key = str(identity_key or (row["identity_key"] if row else "")).strip()
        if not key and payload is None and row:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
        key = key or gaca_identity_key(payload)
        # A legacy row without usable identity data must not stop relatives.
        # Give only that job an isolated circuit instead.
        if not key:
            key = hashlib.sha256(
                f"job:{current_job_id}".encode("utf-8")
            ).hexdigest()[:32]

        first_due = time.time() + delay
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE portal_jobs SET identity_key=? WHERE id=?",
            (key, str(current_job_id)),
        )
        conn.execute(
            """INSERT INTO complaint_response_state (state_key, state_value)
               VALUES (?, ?)
               ON CONFLICT(state_key) DO UPDATE SET
                   state_value=MAX(
                       CAST(complaint_response_state.state_value AS REAL),
                       CAST(excluded.state_value AS REAL)
                   )""",
            (_GACA_IDENTITY_CIRCUIT_PREFIX + key, str(first_due)),
        )
        rows = conn.execute(
            """SELECT id FROM portal_jobs
               WHERE kind='gaca' AND identity_key=? AND terminal=0
                 AND status NOT IN (
                     'submitted', 'superseded', 'cancelled', 'held')
               ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,
                        COALESCE(next_attempt_at, 0), created_at, id""",
            (key, str(current_job_id)),
        ).fetchall()
        for index, active in enumerate(rows):
            due = first_due + index * spacing
            conn.execute(
                """UPDATE portal_jobs
                   SET status='retry_wait', terminal=0, lease_until=NULL,
                       next_attempt_at=?, last_error=?, message=?,
                       updated_at=datetime('now', 'localtime')
                   WHERE id=?""",
                (
                    due,
                    str(error)[:2000],
                    "GACA temporarily limited this passenger identity. "
                    "Only this passenger is paused; the next safe attempt "
                    "is scheduled after the 24-hour cooldown.",
                    str(active["id"]),
                ),
            )
    return first_due


def hold_portal_job(job_id: str, reason: str) -> bool:
    """Keep an unsubmitted job durable without allowing the scheduler to run it."""
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE portal_jobs
               SET status='held', terminal=0, lease_until=NULL,
                   next_attempt_at=NULL, message=?, last_error=?,
                   updated_at=datetime('now', 'localtime')
               WHERE id=? AND terminal=0
                 AND status NOT IN ('submitted', 'superseded', 'cancelled')""",
            (str(reason)[:2000], str(reason)[:2000], str(job_id)),
        )
    return bool(cursor.rowcount)


def get_ai_analysis_cache(cache_key: str, model: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT result FROM ai_analysis_cache
               WHERE cache_key = ? AND model = ?""",
            (cache_key, model)).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result"])
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def save_ai_analysis_cache(cache_key: str, task: str, model: str,
                           result: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO ai_analysis_cache
                   (cache_key, task, model, result, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(cache_key) DO UPDATE SET
                   task=excluded.task, model=excluded.model,
                   result=excluded.result, updated_at=excluded.updated_at""",
            (cache_key, task, model,
             json.dumps(result, ensure_ascii=False)))


def save_email(parsed) -> int:
    """Insert or update a parsed email; returns its row id."""
    record = {k: v for k, v in vars(parsed).items() if k != "body_text"}
    record["date"] = parsed.date.isoformat() if parsed.date else None
    with connect() as conn:
        conn.execute(
            """INSERT INTO emails (message_id, subject, sender, date, kinds, parsed, body)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO UPDATE SET
                 subject=excluded.subject, sender=excluded.sender,
                 date=excluded.date, kinds=excluded.kinds,
                 parsed=excluded.parsed, body=excluded.body""",
            (parsed.message_id, parsed.subject, parsed.sender, record["date"],
             json.dumps(parsed.kinds), json.dumps(record, default=str),
             parsed.body_text),
        )
        row = conn.execute("SELECT id FROM emails WHERE message_id = ?",
                           (parsed.message_id,)).fetchone()
        return row["id"]


def has_email(message_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM emails WHERE message_id = ?",
                            (message_id,)).fetchone() is not None


def all_emails() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM emails ORDER BY date").fetchall()
    out = []
    for row in rows:
        parsed = json.loads(row["parsed"])
        parsed["db_id"] = row["id"]
        parsed["body"] = row["body"]
        out.append(parsed)
    return out


def list_email_summaries() -> list[dict]:
    """Return parsed email metadata without loading large message bodies."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, parsed FROM emails ORDER BY date DESC").fetchall()
    out = []
    for row in rows:
        parsed = json.loads(row["parsed"])
        parsed["db_id"] = row["id"]
        out.append(parsed)
    return out


def identity_suggestions(passenger_name: str) -> dict:
    """Return conflict-free profile fields found in this passenger's tickets.

    Sensitive identifiers are suggested only when every labeled occurrence
    for the passenger agrees.  Contact fields may use the newest occurrence.
    """
    key = passenger_profile_key(passenger_name)
    if not key:
        return {"values": {}, "evidence": {}, "conflicts": []}
    with connect() as conn:
        rows = conn.execute(
            "SELECT parsed, subject FROM emails ORDER BY date DESC, id DESC"
        ).fetchall()
    candidates: dict[str, list[tuple[str, str]]] = {}
    national_candidates: dict[str, list[tuple[str, str]]] = {}
    fields = {
        "title", "nationality", "email", "phone", "country_code",
        "alfursan_id",
    }
    for row in rows:
        parsed = json.loads(row["parsed"] or "{}")
        profiles = parsed.get("passenger_profiles") or {}
        profile = profiles.get(key)
        if not isinstance(profile, dict):
            profile = next((value for value in profiles.values()
                            if isinstance(value, dict)
                            and passenger_profile_key(
                                value.get("booking_name") or
                                value.get("full_name") or "") == key), None)
        if not profile:
            continue
        profile_evidence = profile.get("evidence") or {}
        national_id = str(profile.get("national_id") or "").strip()
        if national_id:
            id_type = str(profile.get("national_id_type") or "national_id")
            source = str(profile_evidence.get("national_id") or "").strip()
            if not source or source == "linked ticket or booking email":
                subject = str(row["subject"] or "ticket or booking email").strip()
                source = f"Email: {subject[:100]}"
            national_candidates.setdefault(id_type, []).append(
                (national_id, source))
        for field in fields:
            value = str(profile.get(field) or "").strip()
            if not value:
                continue
            source = str(profile_evidence.get(field) or "").strip()
            if not source or source == "linked ticket or booking email":
                subject = str(row["subject"] or "ticket or booking email").strip()
                source = f"Email: {subject[:100]}"
            candidates.setdefault(field, []).append((value, source))

    values, evidence, conflicts = {}, {}, set()
    for id_type in ("national_id", "iqama", "passport"):
        occurrences = national_candidates.get(id_type) or []
        if not occurrences:
            continue
        distinct = {value.casefold(): value for value, _source in occurrences}
        if len(distinct) == 1:
            values["national_id"], evidence["national_id"] = occurrences[0]
        else:
            conflicts.add("national_id")
        break
    immutable = {"alfursan_id", "title", "nationality"}
    for field, occurrences in candidates.items():
        distinct = {value.casefold(): value for value, _source in occurrences}
        if field in immutable and len(distinct) != 1:
            conflicts.add(field)
            continue
        value, source = occurrences[0]
        values[field] = value
        evidence[field] = source
    return {"values": values, "evidence": evidence,
            "conflicts": sorted(conflicts)}


def identity_evidence(passenger_name: str, limit: int = 8) -> list[dict]:
    """Load bounded, exact-passenger ticket blocks for Claude extraction."""
    from .parser import passenger_evidence_blocks

    key = passenger_profile_key(passenger_name)
    if not key:
        return []
    with connect() as conn:
        rows = conn.execute(
            """SELECT subject, parsed, body FROM emails
               ORDER BY date DESC, id DESC""").fetchall()
    evidence, seen = [], set()
    for row in rows:
        parsed = json.loads(row["parsed"] or "{}")
        profiles = parsed.get("passenger_profiles") or {}
        profile_match = key in profiles or any(
            isinstance(profile, dict) and passenger_profile_key(
                profile.get("booking_name") or profile.get("full_name") or "") == key
            for profile in profiles.values())
        body = str(row["body"] or "")
        if not profile_match and passenger_name.casefold() not in body.casefold():
            continue
        for block in passenger_evidence_blocks(body, passenger_name, limit=limit):
            text = " ".join(str(block.get("text") or "").split())[:6000]
            fingerprint = text.casefold()
            if not text or fingerprint in seen:
                continue
            seen.add(fingerprint)
            source = str(block.get("source") or "").strip()
            if not source or source == "linked ticket or booking email":
                source = f"Email: {str(row['subject'] or 'ticket or booking email')[:100]}"
            evidence.append({"source": source, "text": text})
            if len(evidence) >= limit:
                return evidence
    return evidence


def get_ai_profile_cache(passenger_name: str, evidence_hash: str,
                         model: str) -> dict | None:
    key = passenger_profile_key(passenger_name)
    with connect() as conn:
        row = conn.execute(
            """SELECT result FROM ai_profile_cache
               WHERE passenger_key = ? AND evidence_hash = ? AND model = ?""",
            (key, evidence_hash, model)).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def save_ai_profile_cache(passenger_name: str, evidence_hash: str,
                          model: str, result: dict) -> None:
    key = passenger_profile_key(passenger_name)
    if not key:
        return
    with connect() as conn:
        conn.execute(
            """INSERT INTO ai_profile_cache
                   (passenger_key, evidence_hash, model, result, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(passenger_key) DO UPDATE SET
                   evidence_hash=excluded.evidence_hash,
                   model=excluded.model,
                   result=excluded.result,
                   updated_at=excluded.updated_at""",
            (key, evidence_hash, model,
             json.dumps(result, ensure_ascii=False)))


def email_flight_map() -> dict[int, dict]:
    """Map stored email ids to their lightweight linked flight record."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT fe.email_id, f.id AS flight_id, f.data
               FROM flight_emails fe
               JOIN flights f ON f.id = fe.flight_id""").fetchall()
    result = {}
    for row in rows:
        flight = json.loads(row["data"])
        flight["id"] = row["flight_id"]
        result[row["email_id"]] = flight
    return result


def counts() -> dict[str, int]:
    with connect() as conn:
        return {
            "emails": conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0],
            "flights": conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0],
            "complaints": conn.execute(
                "SELECT COUNT(*) FROM complaints").fetchone()[0],
        }


def replace_flights(flights: list[dict]):
    """Rewrite the flights table from a fresh linking pass.

    Manual overrides are preserved across rescans (matched by flight_key).
    """
    with connect() as conn:
        existing = {row["flight_key"]: row["overrides"] for row in
                    conn.execute("SELECT flight_key, overrides FROM flights")}
        conn.execute("DELETE FROM flight_emails")
        conn.execute("DELETE FROM flights")
        for flight in flights:
            email_ids = flight.pop("email_ids", [])
            conn.execute(
                "INSERT INTO flights (flight_key, data, overrides) VALUES (?, ?, ?)",
                (flight["flight_key"], json.dumps(flight, default=str),
                 existing.get(flight["flight_key"], "{}")),
            )
            fid = conn.execute("SELECT id FROM flights WHERE flight_key = ?",
                               (flight["flight_key"],)).fetchone()["id"]
            for eid in email_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO flight_emails (flight_id, email_id) VALUES (?, ?)",
                    (fid, eid),
                )


def list_flights() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM flights").fetchall()
    flights = []
    for row in rows:
        flight = json.loads(row["data"])
        flight["id"] = row["id"]
        flight["overrides"] = json.loads(row["overrides"] or "{}")
        flights.append(flight)
    flights.sort(key=lambda f: f.get("flight_date") or f.get("departure") or "",
                 reverse=True)
    return flights


def save_flight_status_observation(observation: dict) -> int:
    """Persist one normalized provider result without duplicating retries."""
    payload = dict(observation.get("data") or {})
    raw_hash = str(observation.get("raw_hash") or "").strip()
    if not raw_hash:
        raise ValueError("A flight-status observation requires raw_hash.")
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO flight_status_observations
               (flight_key, provider, provider_flight_id, status, confidence,
                observed_at, source_timestamp, raw_hash, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation["flight_key"], observation["provider"],
             observation.get("provider_flight_id"), observation["status"],
             float(observation.get("confidence") or 0),
             observation["observed_at"], observation.get("source_timestamp"),
             raw_hash, json.dumps(payload, default=str, sort_keys=True)))
        row = conn.execute(
            """SELECT id FROM flight_status_observations
               WHERE flight_key = ? AND provider = ? AND raw_hash = ?""",
            (observation["flight_key"], observation["provider"], raw_hash),
        ).fetchone()
    return int(row["id"])


def list_flight_status_observations(flight_key: str,
                                    limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM flight_status_observations
               WHERE flight_key = ? ORDER BY observed_at DESC, id DESC LIMIT ?""",
            (flight_key, max(1, min(int(limit), 500))),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item.get("data") or "{}")
        results.append(item)
    return results


def save_flight_status_snapshot(flight_key: str, snapshot: dict) -> None:
    updated_at = str(snapshot.get("updated_at") or datetime.now().isoformat())
    with connect() as conn:
        conn.execute(
            """INSERT INTO flight_status_current (flight_key, snapshot, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(flight_key) DO UPDATE SET
                 snapshot=excluded.snapshot, updated_at=excluded.updated_at""",
            (flight_key, json.dumps(snapshot, default=str, sort_keys=True),
             updated_at))


def get_flight_status_snapshot(flight_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT snapshot FROM flight_status_current WHERE flight_key = ?",
            (flight_key,),
        ).fetchone()
    return json.loads(row["snapshot"]) if row else None


def flight_status_counts() -> dict[str, int]:
    with connect() as conn:
        return {
            "snapshots": conn.execute(
                "SELECT COUNT(*) FROM flight_status_current").fetchone()[0],
            "observations": conn.execute(
                "SELECT COUNT(*) FROM flight_status_observations").fetchone()[0],
        }


def get_flight(flight_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM flights WHERE id = ?",
                           (flight_id,)).fetchone()
        if not row:
            return None
        flight = json.loads(row["data"])
        flight["id"] = row["id"]
        flight["overrides"] = json.loads(row["overrides"] or "{}")
        email_rows = conn.execute(
            """SELECT e.* FROM emails e
               JOIN flight_emails fe ON fe.email_id = e.id
               WHERE fe.flight_id = ? ORDER BY e.date""", (flight_id,)).fetchall()
    flight["emails"] = []
    for erow in email_rows:
        email = json.loads(erow["parsed"])
        email["db_id"] = erow["id"]
        email["body"] = erow["body"]
        flight["emails"].append(email)
    flight["complaints"] = complaints_for_flight(flight.get("flight_key") or "")
    return flight


def get_flight_by_key(flight_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM flights WHERE flight_key = ?", (flight_key,)).fetchone()
    return get_flight(row["id"]) if row else None


def delete_email(email_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))


def add_complaint(flight_key: str, kind: str, to_addr: str | None,
                   subject: str | None, status: str,
                   reference: str | None = None,
                   details: str | None = None,
                   attachments: list[str] | None = None,
                   submitted_text: str | None = None,
                   portal_category: str | None = None,
                   parent_complaint_id: int | None = None,
                   original_text: str | None = None,
                   issue_summary: str | None = None,
                   requested_resolution_summary: str | None = None,
                   submission_source: str = "automation",
                   created_at: str | None = None) -> int:
    with connect() as conn:
        parent = None
        if parent_complaint_id:
            parent = conn.execute(
                "SELECT * FROM complaints WHERE id = ?",
                (int(parent_complaint_id),),
            ).fetchone()
        root_id = (
            int(parent["root_complaint_id"] or parent["id"])
            if parent else None
        )
        generation = int(parent["generation"] or 0) + 1 if parent else 0
        immutable_original = (
            str(parent["original_text"] or parent["details"] or "")
            if parent else str(original_text or details or "")
        )
        cursor = conn.execute(
            """INSERT INTO complaints
               (flight_key, kind, parent_complaint_id, root_complaint_id,
                generation, to_addr, subject, status, reference, details,
                original_text, attachments, submitted_text, portal_category,
                issue_summary, requested_resolution_summary,
                submission_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       COALESCE(?, datetime('now', 'localtime')))""",
            (flight_key, kind, parent_complaint_id, root_id, generation,
             to_addr, subject, status, reference, details,
             immutable_original, json.dumps(attachments or []),
             submitted_text, portal_category, issue_summary,
             requested_resolution_summary,
             str(submission_source or "automation")[:30],
             created_at))
        if not parent:
            conn.execute(
                "UPDATE complaints SET root_complaint_id = ? WHERE id = ?",
                (int(cursor.lastrowid), int(cursor.lastrowid)),
            )
        return int(cursor.lastrowid)


def record_manual_airline_complaint(
        flight_key: str,
        reference: str,
        *,
        details: str = "",
        category: str = "",
        filed_at: str | None = None,
) -> tuple[dict, str]:
    """Attach an externally filed airline case without creating duplicates.

    Returns ``(complaint, outcome)`` where outcome is ``created``,
    ``already_linked``, or ``completed_existing``.  The latter is used when
    FlightDeck already filed the same case and was only waiting for its
    official reference.
    """
    reference = str(reference or "").strip()
    if not reference:
        raise ValueError("A manual complaint reference is required.")
    if filed_at:
        try:
            parsed_filed = datetime.fromisoformat(str(filed_at))
        except ValueError as exc:
            raise ValueError("The manual complaint date is invalid.") from exc
        if parsed_filed > datetime.now() + timedelta(minutes=5):
            raise ValueError("The manual complaint date cannot be in the future.")
        created_at = parsed_filed.strftime("%Y-%m-%d %H:%M:%S")
    else:
        created_at = None
    statement = str(details or "").strip()
    if not statement:
        statement = (
            "Manual airline complaint imported through Telegram. "
            "The original complaint text was not supplied."
        )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            """SELECT * FROM complaints
               WHERE kind = 'airline'
                 AND lower(trim(reference)) = lower(trim(?))
               LIMIT 1""",
            (reference,),
        ).fetchone()
        if duplicate:
            if str(duplicate["flight_key"]) != str(flight_key):
                raise ValueError(
                    "That complaint reference is already linked to another flight.")
            return dict(duplicate), "already_linked"
        active = conn.execute(
            """SELECT * FROM complaints
               WHERE flight_key = ? AND kind = 'airline'
                 AND status IN ('filing', 'submitted', 'filed', 'sent',
                                'accepted_pending_reference')
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (str(flight_key),),
        ).fetchone()
        if active:
            if (active["status"] == "accepted_pending_reference"
                    and not str(active["reference"] or "").strip()):
                conn.execute(
                    """UPDATE complaints
                       SET status='submitted', reference=?
                       WHERE id=?""",
                    (reference, int(active["id"])),
                )
                row = conn.execute(
                    "SELECT * FROM complaints WHERE id=?",
                    (int(active["id"]),),
                ).fetchone()
                return dict(row), "completed_existing"
            raise ValueError(
                "This flight already has an active airline complaint. "
                "I did not create a second case.")
        cursor = conn.execute(
            """INSERT INTO complaints
                   (flight_key, kind, root_complaint_id, generation, subject,
                    status, reference, details, original_text, submitted_text,
                    portal_category, issue_summary, submission_source,
                    created_at)
               VALUES (?, 'airline', NULL, 0, ?, 'submitted', ?, ?, ?, ?, ?,
                       ?, 'manual_telegram',
                       COALESCE(?, datetime('now', 'localtime')))""",
            (
                str(flight_key), "Manually filed airline complaint",
                reference, statement, statement,
                str(details or "").strip() or None,
                str(category or "").strip() or None,
                statement,
                created_at,
            ),
        )
        complaint_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE complaints SET root_complaint_id=? WHERE id=?",
            (complaint_id, complaint_id),
        )
        row = conn.execute(
            "SELECT * FROM complaints WHERE id=?", (complaint_id,)
        ).fetchone()
        return dict(row), "created"


def active_complaint_for_flight(flight_key: str, kind: str) -> dict | None:
    """Return a filing/submitted claim that must not be launched again."""
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM complaints
               WHERE flight_key = ? AND kind = ?
                 AND status IN ('filing', 'submitted', 'filed', 'sent',
                                'accepted_pending_reference')
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (flight_key, kind)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["attachments"] = json.loads(item.get("attachments") or "[]")
    return item


def close_complaint(complaint_id: int, status: str = "closed") -> None:
    """Mark a prior filing closed/resolved so a reopen can start cleanly."""
    status = status if status in {"closed", "resolved"} else "closed"
    with connect() as conn:
        conn.execute(
            "UPDATE complaints SET status = ? WHERE id = ?",
            (status, complaint_id))


def begin_complaint(flight_key: str, kind: str, subject: str | None,
                     details: str | None = None,
                     attachments: list[str] | None = None,
                     submitted_text: str | None = None,
                     portal_category: str | None = None,
                     *, reopen: bool = False,
                     parent_complaint_id: int | None = None,
                     issue_summary: str | None = None,
                     requested_resolution_summary: str | None = None,
                     escalate_parent_on_success: bool = False) -> int | None:
    """Atomically reserve one official filing per flight and destination."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT id, status, created_at FROM complaints
               WHERE flight_key = ? AND kind = ?
                 AND status IN ('filing', 'submitted', 'filed', 'sent',
                                'accepted_pending_reference')
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (flight_key, kind)).fetchone()
        if current:
            stale_by_age = conn.execute(
                """SELECT 1 WHERE ? = 'filing'
                   AND datetime(?) < datetime('now', 'localtime', '-45 minutes')""",
                (current["status"], current["created_at"])).fetchone()
            placeholders = ",".join(
                "?" for _ in _ACTIVE_PORTAL_JOB_STATUSES)
            active_portal_job = conn.execute(
                f"""SELECT 1 FROM portal_jobs
                    WHERE complaint_id = ?
                      AND terminal = 0
                      AND status IN ({placeholders})
                    LIMIT 1""",
                (
                    int(current["id"]),
                    *_ACTIVE_PORTAL_JOB_STATUSES,
                ),
            ).fetchone()
            if stale_by_age and not active_portal_job:
                conn.execute(
                    "UPDATE complaints SET status = 'interrupted' WHERE id = ?",
                    (current["id"],))
            elif reopen and current["status"] in {
                    "submitted", "filed", "sent", "accepted_pending_reference"}:
                # Explicit reopen after a closed/resolved cycle: retire the
                # prior active row so a fresh filing can be reserved.
                conn.execute(
                    "UPDATE complaints SET status = 'closed' WHERE id = ?",
                    (current["id"],))
            else:
                return None
        parent = None
        if parent_complaint_id:
            parent = conn.execute(
                "SELECT * FROM complaints WHERE id = ? AND flight_key = ?",
                (int(parent_complaint_id), flight_key),
            ).fetchone()
            if not parent:
                raise ValueError("Complaint parent does not belong to this flight")
        root_id = (
            int(parent["root_complaint_id"] or parent["id"])
            if parent else None
        )
        generation = int(parent["generation"] or 0) + 1 if parent else 0
        original_text = (
            str(parent["original_text"] or parent["details"] or "")
            if parent else str(details or "")
        )
        cursor = conn.execute(
            """INSERT INTO complaints
               (flight_key, kind, parent_complaint_id, root_complaint_id,
                generation, subject, status, details, original_text,
                attachments, submitted_text, portal_category, issue_summary,
                requested_resolution_summary, escalate_parent_on_success)
               VALUES (?, ?, ?, ?, ?, ?, 'filing', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (flight_key, kind, parent_complaint_id, root_id, generation,
             subject, details, original_text,
             json.dumps(attachments or []), submitted_text, portal_category,
             issue_summary, requested_resolution_summary,
             int(bool(escalate_parent_on_success))))
        complaint_id = int(cursor.lastrowid)
        if not parent:
            conn.execute(
                "UPDATE complaints SET root_complaint_id = ? WHERE id = ?",
                (complaint_id, complaint_id),
            )
        return complaint_id


def finish_complaint(complaint_id: int, status: str,
                     reference: str | None = None,
                     *, submitted_text: str | None = None,
                     portal_category: str | None = None) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT flight_key FROM complaints WHERE id = ?",
            (complaint_id,)).fetchone()
        conn.execute(
            """UPDATE complaints SET status = ?,
               reference = COALESCE(?, reference),
               submitted_text = COALESCE(?, submitted_text),
               portal_category = COALESCE(?, portal_category)
               WHERE id = ?""",
            (status, reference, submitted_text, portal_category, complaint_id))
    if (status == "submitted" and reference and row
            and row["flight_key"]):
        mark_portal_jobs_reference_recovered(row["flight_key"], reference)


def get_complaint(complaint_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM complaints WHERE id = ?",
            (int(complaint_id),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["attachments"] = json.loads(item.get("attachments") or "[]")
    return item


def set_complaint_response(
        complaint_id: int,
        *,
        response_text: str,
        response_summary: str = "",
) -> None:
    """Retain both the provider's raw reply and Ghala's factual summary."""
    with connect() as conn:
        conn.execute(
            """UPDATE complaints
               SET provider_response_text = ?,
                   response_summary = ?
               WHERE id = ?""",
            (str(response_text or ""), str(response_summary or ""),
             int(complaint_id)),
        )


def set_complaint_summaries(
        complaint_id: int,
        *,
        issue_summary: str,
        requested_resolution_summary: str,
) -> None:
    """Update dashboard-only summaries; immutable original text is untouched."""
    with connect() as conn:
        conn.execute(
            """UPDATE complaints
               SET issue_summary = ?,
                   requested_resolution_summary = ?
               WHERE id = ?""",
            (
                str(issue_summary or "").strip(),
                str(requested_resolution_summary or "").strip(),
                int(complaint_id),
            ),
        )


def clear_parent_escalation_flag(complaint_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE complaints
               SET escalate_parent_on_success = 0
               WHERE id = ?""",
            (int(complaint_id),),
        )


def pending_parent_escalations() -> list[dict]:
    """Airline follow-ups accepted by the provider whose parent needs GACA."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*
               FROM complaints c
               WHERE c.kind = 'airline'
                 AND c.escalate_parent_on_success = 1
                 AND c.parent_complaint_id IS NOT NULL
                 AND c.status IN (
                     'submitted', 'accepted_pending_reference', 'filed', 'sent'
                 )
               ORDER BY c.created_at, c.id"""
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["attachments"] = json.loads(item.get("attachments") or "[]")
        result.append(item)
    return result


def complaints_for_flight(flight_key: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM complaints WHERE flight_key = ?
               ORDER BY created_at""", (flight_key,)).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["attachments"] = json.loads(item.get("attachments") or "[]")
        results.append(item)
    return _decorate_complaint_lineage(results)


def _decorate_complaint_lineage(items: list[dict]) -> list[dict]:
    by_id = {int(item["id"]): item for item in items}
    children: dict[int, list[dict]] = {}
    for item in items:
        parent_id = int(item.get("parent_complaint_id") or 0)
        if parent_id:
            children.setdefault(parent_id, []).append(item)
    for item in items:
        parent = by_id.get(int(item.get("parent_complaint_id") or 0))
        root = by_id.get(int(item.get("root_complaint_id") or item["id"]))
        item["parent_reference"] = (
            str(parent.get("reference") or f"case #{parent['id']}")
            if parent else ""
        )
        item["root_reference"] = (
            str(root.get("reference") or f"case #{root['id']}")
            if root else f"case #{item['id']}"
        )
        item["child_complaints"] = [
            {
                "id": int(child["id"]),
                "kind": child.get("kind"),
                "status": child.get("status"),
                "reference": child.get("reference"),
            }
            for child in children.get(int(item["id"]), [])
        ]
    return items


def list_complaints() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, f.id AS flight_id, f.data AS flight_data,
                      f.overrides AS flight_overrides
               FROM complaints c
               LEFT JOIN flights f ON f.flight_key = c.flight_key
               ORDER BY c.created_at DESC, c.id DESC""").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["attachments"] = json.loads(item.get("attachments") or "[]")
        item["flight_data"] = (json.loads(item["flight_data"])
                               if item.get("flight_data") else {})
        item["flight_data"]["overrides"] = (
            json.loads(item.get("flight_overrides") or "{}"))
        item.pop("flight_overrides", None)
        results.append(item)
    return _decorate_complaint_lineage(results)


def survey_for_flight(flight_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_surveys WHERE flight_key = ?",
            (flight_key,)).fetchone()
    return dict(row) if row else None


def record_survey(flight_key: str, chat_id: str, prompt_message_id: int,
                  status: str = "asked"):
    with connect() as conn:
        conn.execute(
            """INSERT INTO telegram_surveys
               (flight_key, chat_id, prompt_message_id, status)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(flight_key) DO UPDATE SET
                 chat_id=excluded.chat_id,
                 prompt_message_id=excluded.prompt_message_id,
                 status=excluded.status,
                 updated_at=datetime('now', 'localtime')""",
            (flight_key, str(chat_id), prompt_message_id, status))


def update_survey_status(flight_key: str, status: str):
    with connect() as conn:
        conn.execute(
            """UPDATE telegram_surveys SET status = ?,
               updated_at = datetime('now', 'localtime') WHERE flight_key = ?""",
            (status, flight_key))


def delete_survey(flight_key: str) -> None:
    """Remove a retracted prompt so the correct flight can be checked later."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM telegram_surveys WHERE flight_key = ?",
            (flight_key,))


def pending_survey(chat_id: str, reply_to_message_id: int | None = None) -> dict | None:
    query = ("SELECT * FROM telegram_surveys WHERE chat_id = ? "
             "AND status IN ('asked', 'awaiting_details', 'collecting')")
    params: list = [str(chat_id)]
    if reply_to_message_id is not None:
        query += " AND prompt_message_id = ?"
        params.append(reply_to_message_id)
    query += " ORDER BY updated_at DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def event_seen(event_key: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM telegram_events WHERE event_key = ?",
            (event_key,)).fetchone() is not None


def list_event_keys() -> set[str]:
    """Load one monitor-cycle snapshot of all one-shot event markers."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_key FROM telegram_events").fetchall()
    return {str(row["event_key"]) for row in rows}


def mark_event_seen(event_key: str) -> bool:
    """Atomically claim a one-shot event; true only for the first caller."""
    with connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO telegram_events (event_key) VALUES (?)",
            (event_key,))
    return bool(cursor.rowcount)


def clear_event_seen(event_key: str) -> bool:
    """Forget a one-shot marker so automation can retry after a failed job."""
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM telegram_events WHERE event_key = ?",
            (event_key,))
        return cursor.rowcount > 0


def record_telegram_message(direction: str, message_id: int | None,
                            text: str = "", media_kind: str = "",
                            reply_to_message_id: int | None = None) -> None:
    """Keep a bounded-source conversation journal for grounded bot context."""
    direction = "incoming" if direction == "incoming" else "outgoing"
    clean_text = " ".join(str(text or "").split())[:4096]
    with connect() as conn:
        conn.execute(
            """INSERT INTO telegram_messages
                   (direction, message_id, text, media_kind,
                    reply_to_message_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(direction, message_id) DO UPDATE SET
                   text=excluded.text,
                   media_kind=excluded.media_kind,
                   reply_to_message_id=excluded.reply_to_message_id""",
            (direction, message_id, clean_text,
             str(media_kind or "")[:40], reply_to_message_id))


def list_telegram_messages(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM telegram_messages
               ORDER BY created_at DESC, id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in reversed(rows)]


def create_ticket_import(
        token: str,
        chat_id: str,
        source_message_id: int,
        source_kind: str,
        *,
        source_file: str = "",
        source_text: str = "",
) -> dict:
    """Create or return the durable draft for one Telegram source message."""
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO telegram_ticket_imports
                   (token, chat_id, source_message_id, source_kind,
                    source_file, source_text, status)
               VALUES (?, ?, ?, ?, ?, ?, 'processing')""",
            (
                str(token), str(chat_id), int(source_message_id),
                str(source_kind or "text")[:30],
                str(source_file or "") or None,
                str(source_text or "")[:60_000],
            ),
        )
        row = conn.execute(
            """SELECT * FROM telegram_ticket_imports
               WHERE chat_id = ? AND source_message_id = ?""",
            (str(chat_id), int(source_message_id)),
        ).fetchone()
    return _ticket_import_row(row)


def _ticket_import_row(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for key, fallback in (("extracted", {}), ("imported_flight_keys", [])):
        try:
            item[key] = json.loads(item.get(key) or json.dumps(fallback))
        except (TypeError, ValueError):
            item[key] = fallback
    return item


def get_ticket_import(token: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_ticket_imports WHERE token = ?",
            (str(token),),
        ).fetchone()
    return _ticket_import_row(row)


def latest_pending_ticket_import(chat_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM telegram_ticket_imports
               WHERE chat_id = ?
                 AND status IN ('processing', 'awaiting_details', 'ready')
               ORDER BY updated_at DESC, created_at DESC LIMIT 1""",
            (str(chat_id),),
        ).fetchone()
    return _ticket_import_row(row)


def update_ticket_import(
        token: str,
        *,
        status: str | None = None,
        source_file: str | None = None,
        source_text: str | None = None,
        extracted: dict | None = None,
        imported_flight_keys: list[str] | None = None,
        error: str | None = None,
) -> bool:
    """Update a draft while preserving fields omitted by the caller."""
    assignments = ["updated_at=datetime('now', 'localtime')"]
    values: list = []
    for column, value in (
        ("status", status),
        ("source_file", source_file),
        ("source_text", source_text),
        ("extracted", (
            json.dumps(extracted, ensure_ascii=False)
            if extracted is not None else None)),
        ("imported_flight_keys", (
            json.dumps(imported_flight_keys, ensure_ascii=False)
            if imported_flight_keys is not None else None)),
        ("error", error),
    ):
        if value is None:
            continue
        assignments.append(f"{column} = ?")
        values.append(value)
    values.append(str(token))
    with connect() as conn:
        cursor = conn.execute(
            f"""UPDATE telegram_ticket_imports SET {', '.join(assignments)}
                WHERE token = ?""",
            values,
        )
    return bool(cursor.rowcount)


def save_portal_job(job: dict) -> None:
    """Persist portal stages, replay payload, and retry state."""
    payload = job.get("payload")
    if isinstance(payload, str):
        payload_json = payload
    elif payload is None:
        payload_json = None
    else:
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"))
    parsed_payload = payload if isinstance(payload, dict) else {}
    if not parsed_payload and isinstance(payload, str):
        try:
            parsed_payload = json.loads(payload)
        except (TypeError, ValueError):
            parsed_payload = {}
    kind = str(job.get("kind") or parsed_payload.get("kind") or "")
    identity_key = (
        gaca_identity_key(parsed_payload) if kind == "gaca" else "")
    with connect() as conn:
        conn.execute(
            """INSERT INTO portal_jobs
                   (id, kind, identity_key, airline_code, flight_number, flight_key,
                    complaint_id, payload, status, message, reference,
                    screenshot_file, terminal, attempts, max_attempts,
                    next_attempt_at, lease_until, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, '{}'), ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   status=excluded.status,
                   message=excluded.message,
                   reference=excluded.reference,
                   screenshot_file=COALESCE(excluded.screenshot_file,
                                            portal_jobs.screenshot_file),
                   terminal=excluded.terminal,
                   complaint_id=COALESCE(excluded.complaint_id,
                                         portal_jobs.complaint_id),
                   identity_key=COALESCE(
                       NULLIF(excluded.identity_key, ''),
                       portal_jobs.identity_key),
                   payload=CASE
                       WHEN excluded.payload IS NOT NULL
                            AND excluded.payload != '{}'
                       THEN excluded.payload ELSE portal_jobs.payload END,
                   attempts=MAX(portal_jobs.attempts, excluded.attempts),
                   max_attempts=excluded.max_attempts,
                   next_attempt_at=excluded.next_attempt_at,
                   lease_until=excluded.lease_until,
                   last_error=excluded.last_error,
                   updated_at=datetime('now', 'localtime')""",
            (str(job.get("id") or ""), kind, identity_key or None,
             str(job.get("airline_code") or ""),
             str(job.get("flight_number") or ""),
             str(job.get("flight_key") or ""),
             job.get("complaint_id"),
             payload_json,
             str(job.get("status") or "queued"),
             str(job.get("message") or "")[:2000],
             str(job.get("reference") or ""),
             str(job.get("screenshot_file") or "") or None,
             int(bool(job.get("terminal"))),
             int(job.get("attempts") or 0),
             max(1, int(job.get("max_attempts") or 100000)),
             job.get("next_attempt_at"),
             job.get("lease_until"),
             str(job.get("last_error") or "")[:2000] or None))


def get_portal_job(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM portal_jobs WHERE id = ?", (str(job_id),)
        ).fetchone()
    if not row:
        return None
    job = dict(row)
    try:
        job["payload"] = json.loads(job.get("payload") or "{}")
    except (TypeError, ValueError):
        job["payload"] = {}
    return job


def active_portal_job_for_complaint(complaint_id: int) -> dict | None:
    """Return the authoritative non-terminal portal worker for a complaint."""
    placeholders = ",".join("?" for _ in _ACTIVE_PORTAL_JOB_STATUSES)
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id FROM portal_jobs
                WHERE complaint_id = ?
                  AND terminal = 0
                  AND status IN ({placeholders})
                ORDER BY created_at, id
                LIMIT 1""",
            (int(complaint_id), *_ACTIVE_PORTAL_JOB_STATUSES),
        ).fetchone()
    return get_portal_job(str(row["id"])) if row else None


def latest_portal_job_for_complaint(complaint_id: int) -> dict | None:
    """Return the newest portal job, including a completed one."""
    with connect() as conn:
        row = conn.execute(
            """SELECT id FROM portal_jobs
               WHERE complaint_id = ?
               ORDER BY updated_at DESC, created_at DESC, id DESC
               LIMIT 1""",
            (int(complaint_id),),
        ).fetchone()
    return get_portal_job(str(row["id"])) if row else None


def gaca_confirmation_unknown_complaint_ids() -> set[int]:
    """Return GACA complaints accepted or ambiguously sent without a reference.

    These are safe reconciliation targets for a later GACA SMS/email
    reference, but are deliberately not safe targets for resubmission.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.complaint_id
               FROM portal_jobs p
               JOIN complaints c ON c.id = p.complaint_id
               WHERE p.kind = 'gaca'
                  AND p.status IN (
                      'confirmation_unknown', 'accepted_pending_reference')
                 AND c.kind = 'gaca'
                 AND COALESCE(c.reference, '') = ''
                 AND NOT EXISTS (
                     SELECT 1
                     FROM portal_jobs newer
                     WHERE newer.complaint_id = p.complaint_id
                       AND (
                           newer.created_at > p.created_at
                           OR (
                               newer.created_at = p.created_at
                               AND newer.id > p.id
                           )
                       )
                 )"""
        ).fetchall()
    return {
        int(row["complaint_id"])
        for row in rows
        if row["complaint_id"] is not None
    }


def reconcile_portal_confirmation(complaint_id: int, reference: str) -> bool:
    """Atomically attach a regulator reference to a sent GACA complaint."""
    reference = str(reference or "").strip()
    if not reference:
        return False
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        eligible = conn.execute(
            """SELECT 1
               FROM complaints c
               JOIN portal_jobs p ON p.complaint_id = c.id
               WHERE c.id = ?
                 AND c.kind = 'gaca'
                 AND COALESCE(c.reference, '') = ''
                 AND p.kind = 'gaca'
                  AND p.status IN (
                      'confirmation_unknown', 'accepted_pending_reference')
               LIMIT 1""",
            (int(complaint_id),),
        ).fetchone()
        if not eligible:
            return False
        duplicate = conn.execute(
            """SELECT 1 FROM complaints
               WHERE reference = ? COLLATE NOCASE AND id != ?
               LIMIT 1""",
            (reference, int(complaint_id)),
        ).fetchone()
        if duplicate:
            return False
        conn.execute(
            """UPDATE complaints
               SET status='submitted', reference=?
               WHERE id=?""",
            (reference, int(complaint_id)),
        )
        conn.execute(
            """UPDATE portal_jobs
               SET status='submitted', terminal=1, reference=?,
                   next_attempt_at=NULL, lease_until=NULL, last_error=NULL,
                   message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE complaint_id=?
                 AND kind='gaca'
                  AND status IN (
                      'confirmation_unknown', 'accepted_pending_reference')""",
            (
                reference,
                "GACA reference reconciled from an explicit regulator "
                "acknowledgement; no duplicate submission was made.",
                int(complaint_id),
            ),
        )
    return True


def reconcile_airline_portal_acceptance(
        job_id: str,
        *,
        accepted_at: str = "",
        message: str = "") -> bool:
    """Record a readable airline acceptance that returned no reference.

    Only an airline job already quarantined after Submit (or marked
    confirmation-unknown) is eligible, and neither record may already own a
    reference. The caller must have official-page evidence of acceptance.
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT p.complaint_id, p.updated_at
               FROM portal_jobs p
               JOIN complaints c ON c.id=p.complaint_id
               WHERE p.id=?
                 AND p.kind='airline'
                 AND p.status IN ('quarantined', 'confirmation_unknown')
                 AND c.kind='airline'
                 AND COALESCE(p.reference, '')=''
                 AND COALESCE(c.reference, '')=''""",
            (str(job_id),),
        ).fetchone()
        if not row:
            return False
        timestamp = str(accepted_at or row["updated_at"] or "").strip()
        acceptance_message = str(message or "").strip() or (
            "The airline displayed a readable acceptance confirmation. "
            "FlightDeck is waiting for the public case reference and will "
            "not submit a duplicate."
        )
        conn.execute(
            """UPDATE complaints
               SET status='accepted_pending_reference',
                   created_at=COALESCE(NULLIF(?, ''), created_at)
               WHERE id=?""",
            (timestamp, int(row["complaint_id"])),
        )
        conn.execute(
            """UPDATE portal_jobs
               SET status='accepted_pending_reference', terminal=1,
                   message=?, next_attempt_at=NULL, lease_until=NULL,
                   last_error=NULL,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (acceptance_message, str(job_id)),
        )
    return True


def upsert_gaca_account_case(case: dict, mapping: dict | None = None) -> bool:
    """Persist one read-only case imported from the signed-in GACA account."""
    mapping = dict(mapping or {})
    reference = str(case.get("reference") or "").strip().upper()
    source_url = str(case.get("source_url") or "").strip()
    case_key = str(case.get("case_key") or reference or "").strip()
    if not case_key:
        fingerprint = json.dumps(
            case, ensure_ascii=False, sort_keys=True, default=str)
        case_key = "account:" + hashlib.sha256(
            fingerprint.encode("utf-8")).hexdigest()[:32]
    raw = case.get("raw")
    if not isinstance(raw, dict):
        raw = {
            key: value for key, value in case.items()
            if key not in {"raw", "case_key"}
        }
    fields = (
        "status", "service_type", "airline", "flight_number", "flight_date",
        "airline_reference", "ticket_number", "pnr", "passenger_name",
        "origin", "destination", "category", "submitted_at", "complaint_text",
    )
    values = {
        field: str(case.get(field) or "").strip()
        for field in fields
    }
    mapped_id = mapping.get("complaint_id")
    if mapped_id not in (None, ""):
        mapped_id = int(mapped_id)
    mapped_key = str(mapping.get("flight_key") or "").strip() or None
    mapping_status = str(
        mapping.get("status") or (
            "mapped" if mapped_id else "unmapped"
        )
    ).strip()
    with connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM gaca_account_cases WHERE case_key=?",
            (case_key,),
        ).fetchone()
        conn.execute(
            """INSERT INTO gaca_account_cases (
                   case_key, reference, status, service_type, airline,
                   flight_number, flight_date, airline_reference,
                   ticket_number, pnr, passenger_name, origin, destination,
                   category, submitted_at, complaint_text, source_url,
                   raw_json, mapped_complaint_id, mapped_flight_key,
                   mapping_status, match_method, match_score
               ) VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?
               )
               ON CONFLICT(case_key) DO UPDATE SET
                   reference=COALESCE(NULLIF(excluded.reference, ''), reference),
                   status=COALESCE(NULLIF(excluded.status, ''), status),
                   service_type=COALESCE(
                       NULLIF(excluded.service_type, ''), service_type),
                   airline=COALESCE(NULLIF(excluded.airline, ''), airline),
                   flight_number=COALESCE(
                       NULLIF(excluded.flight_number, ''), flight_number),
                   flight_date=COALESCE(
                       NULLIF(excluded.flight_date, ''), flight_date),
                   airline_reference=COALESCE(
                       NULLIF(excluded.airline_reference, ''),
                       airline_reference),
                   ticket_number=COALESCE(
                       NULLIF(excluded.ticket_number, ''), ticket_number),
                   pnr=COALESCE(NULLIF(excluded.pnr, ''), pnr),
                   passenger_name=COALESCE(
                       NULLIF(excluded.passenger_name, ''), passenger_name),
                   origin=COALESCE(NULLIF(excluded.origin, ''), origin),
                   destination=COALESCE(
                       NULLIF(excluded.destination, ''), destination),
                   category=COALESCE(NULLIF(excluded.category, ''), category),
                   submitted_at=COALESCE(
                       NULLIF(excluded.submitted_at, ''), submitted_at),
                   complaint_text=COALESCE(
                       NULLIF(excluded.complaint_text, ''), complaint_text),
                   source_url=COALESCE(
                       NULLIF(excluded.source_url, ''), source_url),
                   raw_json=excluded.raw_json,
                   mapped_complaint_id=excluded.mapped_complaint_id,
                   mapped_flight_key=NULLIF(
                       excluded.mapped_flight_key, ''),
                   mapping_status=excluded.mapping_status,
                   match_method=excluded.match_method,
                   match_score=excluded.match_score,
                   last_seen_at=datetime('now', 'localtime')""",
            (
                case_key, reference, values["status"], values["service_type"],
                values["airline"], values["flight_number"],
                values["flight_date"], values["airline_reference"],
                values["ticket_number"], values["pnr"],
                values["passenger_name"], values["origin"],
                values["destination"], values["category"],
                values["submitted_at"], values["complaint_text"], source_url,
                json.dumps(raw, ensure_ascii=False, default=str),
                mapped_id, mapped_key, mapping_status,
                str(mapping.get("method") or "").strip(),
                int(mapping.get("score") or 0),
            ),
        )
    return not bool(existed)


def list_gaca_account_cases(limit: int = 500) -> list[dict]:
    """Return imported GACA cases, including their mapped local flight."""
    limit = max(1, min(int(limit or 500), 2000))
    with connect() as conn:
        rows = conn.execute(
            """SELECT gac.*, f.id AS flight_id, f.data AS flight_data,
                      c.status AS local_complaint_status,
                      c.reference AS local_complaint_reference
               FROM gaca_account_cases gac
               LEFT JOIN flights f ON f.flight_key=gac.mapped_flight_key
               LEFT JOIN complaints c ON c.id=gac.mapped_complaint_id
               ORDER BY COALESCE(gac.submitted_at, gac.last_seen_at) DESC,
                        gac.case_key DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
        except (TypeError, ValueError):
            item["raw"] = {}
            item.pop("raw_json", None)
        try:
            item["flight_data"] = (
                json.loads(item["flight_data"])
                if item.get("flight_data") else {}
            )
        except (TypeError, ValueError):
            item["flight_data"] = {}
        result.append(item)
    return result


def get_gaca_account_sync() -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM gaca_account_sync WHERE id=1").fetchone()
    return dict(row) if row else {
        "status": "never_synced", "message": "",
        "cases_seen": 0, "cases_mapped": 0,
        "cases_reconciled": 0, "cases_ambiguous": 0,
        "last_attempt_at": None, "last_success_at": None,
        "screenshot_file": None,
    }


def save_gaca_account_sync(
        status: str,
        message: str,
        *,
        cases_seen: int = 0,
        cases_mapped: int = 0,
        cases_reconciled: int = 0,
        cases_ambiguous: int = 0,
        screenshot_file: str | None = None,
) -> None:
    succeeded = str(status) == "success"
    with connect() as conn:
        conn.execute(
            """INSERT INTO gaca_account_sync (
                   id, status, message, cases_seen, cases_mapped,
                   cases_reconciled, cases_ambiguous, last_attempt_at,
                   last_success_at, screenshot_file
               ) VALUES (
                   1, ?, ?, ?, ?, ?, ?,
                   datetime('now', 'localtime'),
                   CASE WHEN ? THEN datetime('now', 'localtime') END,
                   ?
               )
               ON CONFLICT(id) DO UPDATE SET
                   status=excluded.status,
                   message=excluded.message,
                   cases_seen=excluded.cases_seen,
                   cases_mapped=excluded.cases_mapped,
                   cases_reconciled=excluded.cases_reconciled,
                   cases_ambiguous=excluded.cases_ambiguous,
                   last_attempt_at=excluded.last_attempt_at,
                   last_success_at=CASE
                       WHEN ? THEN excluded.last_success_at
                       ELSE gaca_account_sync.last_success_at
                   END,
                   screenshot_file=COALESCE(
                       excluded.screenshot_file,
                       gaca_account_sync.screenshot_file)""",
            (
                str(status), str(message)[:2000], int(cases_seen),
                int(cases_mapped), int(cases_reconciled),
                int(cases_ambiguous), int(succeeded), screenshot_file,
                int(succeeded),
            ),
        )


def reconcile_gaca_account_case(
        case_key: str,
        complaint_id: int,
        reference: str,
) -> bool:
    """Use a signed-in GACA account record as authoritative acceptance."""
    reference = str(reference or "").strip().upper()
    if not reference:
        return False
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM complaints WHERE id=? AND kind='gaca'",
            (int(complaint_id),),
        ).fetchone()
        imported = conn.execute(
            "SELECT * FROM gaca_account_cases WHERE case_key=?",
            (str(case_key),),
        ).fetchone()
        if not target or not imported:
            return False
        duplicate = conn.execute(
            """SELECT id FROM complaints
               WHERE kind='gaca'
                 AND lower(trim(reference))=lower(trim(?))
                 AND id != ?
               LIMIT 1""",
            (reference, int(complaint_id)),
        ).fetchone()
        if duplicate:
            return False
        conn.execute(
            """UPDATE complaints
               SET status='submitted', reference=?
               WHERE id=?""",
            (reference, int(complaint_id)),
        )
        conn.execute(
            """UPDATE portal_jobs
               SET status='submitted', terminal=1, reference=?,
                   next_attempt_at=NULL, lease_until=NULL, last_error=NULL,
                   message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE complaint_id=? AND kind='gaca'
                 AND status NOT IN ('superseded', 'cancelled')""",
            (
                reference,
                "Reconciled from the signed-in GACA account. The regulator "
                "case exists, so no duplicate submission was made.",
                int(complaint_id),
            ),
        )
        conn.execute(
            """UPDATE gaca_account_cases
               SET reference=?, mapped_complaint_id=?,
                   mapped_flight_key=?, mapping_status='reconciled',
                   last_seen_at=datetime('now', 'localtime')
               WHERE case_key=?""",
            (
                reference, int(complaint_id), str(target["flight_key"] or ""),
                str(case_key),
            ),
        )
    return True


def claim_portal_job(job_id: str, lease_seconds: int = 20 * 60) -> dict | None:
    """Atomically lease one queued portal job for a browser worker."""
    now = time.time()
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE portal_jobs
               SET status='leased', terminal=0, attempts=attempts + 1,
                   lease_until=?, next_attempt_at=NULL,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?
                 AND attempts < max_attempts
                 AND status IN ('queued', 'retry_wait')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 AND (
                     kind != 'gaca'
                     OR COALESCE(identity_key, '')=''
                     OR NOT EXISTS (
                         SELECT 1
                         FROM complaint_response_state circuit
                         WHERE circuit.state_key=? || portal_jobs.identity_key
                           AND CAST(circuit.state_value AS REAL) > ?
                     )
                 )""",
            (
                now + max(60, int(lease_seconds)),
                str(job_id),
                now,
                _GACA_IDENTITY_CIRCUIT_PREFIX,
                now,
            ))
        if not cursor.rowcount:
            return None
    return get_portal_job(job_id)


def list_due_portal_jobs(limit: int = 3) -> list[dict]:
    now = time.time()
    with connect() as conn:
        rows = conn.execute(
            """SELECT id FROM portal_jobs
               WHERE status IN ('queued', 'retry_wait')
                 AND terminal=0
                 AND attempts < max_attempts
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 AND (
                     kind != 'gaca'
                     OR COALESCE(identity_key, '')=''
                     OR NOT EXISTS (
                         SELECT 1
                         FROM complaint_response_state circuit
                         WHERE circuit.state_key=? || portal_jobs.identity_key
                           AND CAST(circuit.state_value AS REAL) > ?
                     )
                 )
               ORDER BY COALESCE(next_attempt_at, 0), created_at, id
               LIMIT ?""",
            (
                now,
                _GACA_IDENTITY_CIRCUIT_PREFIX,
                now,
                max(1, min(int(limit or 3), 20)),
            ),
        ).fetchall()
    return [
        job for row in rows
        if (job := get_portal_job(str(row["id"]))) is not None
    ]


def retry_portal_job(job_id: str, error: str,
                     minimum_delay: int = 5 * 60,
                     fixed_delay: int | None = None) -> float | None:
    """Schedule a confirmed pre-submit failure with exponential backoff."""
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts,max_attempts FROM portal_jobs WHERE id=?",
            (str(job_id),)).fetchone()
        if not row:
            return None
        attempts = int(row["attempts"] or 1)
        if attempts >= int(row["max_attempts"] or 100000):
            conn.execute(
                """UPDATE portal_jobs
                   SET status='quarantined', terminal=1, lease_until=NULL,
                       last_error=?, message=?,
                       updated_at=datetime('now', 'localtime')
                   WHERE id=?""",
                (str(error)[:2000],
                 "Safe retry limit reached; manual review is required.",
                 str(job_id)))
            return None
        if fixed_delay is None:
            delay = min(
                6 * 3600,
                max(int(minimum_delay), 60 * (2 ** attempts)),
            )
        else:
            # A confirmed environmental remediation (for example, replacing
            # a WAF-bound proxy session before Submit) should be retried after
            # a short settling period instead of inheriting outage backoff
            # from earlier, unrelated attempts.
            delay = max(60, min(int(fixed_delay), 6 * 3600))
        next_attempt = time.time() + delay
        conn.execute(
            """UPDATE portal_jobs
               SET status='retry_wait', terminal=0, lease_until=NULL,
                   next_attempt_at=?, last_error=?, message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (next_attempt, str(error)[:2000],
             f"Safely queued to retry after a pre-submit failure "
             f"(attempt {attempts}).",
             str(job_id)))
        return next_attempt


def quarantine_portal_job(job_id: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE portal_jobs
               SET status='quarantined', terminal=1, lease_until=NULL,
                   next_attempt_at=NULL, last_error=?, message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (str(error)[:2000],
             "The previous run may have reached Submit. It is quarantined "
             "until email/SMS/portal state is reconciled.",
             str(job_id)))


def recover_interrupted_portal_jobs() -> dict[str, int]:
    """Recover crashes; GACA Submit interruptions reconcile then retry."""
    now = time.time()
    safe_stages = (
        "leased", "opening", "filling", "reviewing", "verification",
    )
    with connect() as conn:
        placeholders = ",".join("?" for _ in safe_stages)
        safe = conn.execute(
            f"""UPDATE portal_jobs
                SET status='retry_wait', terminal=0, lease_until=NULL,
                    next_attempt_at=?,
                    last_error='process stopped before Submit; safe retry queued',
                    updated_at=datetime('now', 'localtime')
                WHERE status IN ({placeholders})""",
            (now + 60, *safe_stages)).rowcount
        gaca_submit = conn.execute(
            """UPDATE portal_jobs
               SET status='retry_wait', terminal=0, lease_until=NULL,
                   next_attempt_at=?,
                   last_error='process stopped during GACA Submit; no verified acceptance',
                   message=?,
                   updated_at=datetime('now', 'localtime')
               WHERE status='submitting' AND kind='gaca'""",
            (
                now + 15 * 60,
                "GACA acceptance was not verified. Email/SMS will be "
                "reconciled before the durable retry.",
            ),
        ).rowcount
        ambiguous = conn.execute(
            """UPDATE portal_jobs
               SET status='quarantined', terminal=1, lease_until=NULL,
                   next_attempt_at=NULL,
                   last_error='process stopped during Submit; reconcile first',
                   message='Interrupted during Submit; duplicate-safe quarantine.',
                   updated_at=datetime('now', 'localtime')
               WHERE status='submitting' AND kind!='gaca'""").rowcount
    return {"retry_wait": int(safe or 0) + int(gaca_submit or 0),
            "quarantined": int(ambiguous or 0)}


def mark_portal_jobs_reference_recovered(flight_key: str,
                                         reference: str) -> int:
    """Flip stale accepted_pending_reference jobs to success once a ref arrives."""
    flight_key = str(flight_key or "").strip()
    reference = str(reference or "").strip()
    if not flight_key or not reference:
        return 0
    message = (
        f"Airline reference {reference} was recovered after acceptance. "
        "Portal job updated from accepted_pending_reference to success.")
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE portal_jobs
               SET status = 'success',
                   reference = ?,
                   message = ?,
                   terminal = 1,
                   next_attempt_at = NULL,
                   lease_until = NULL,
                   last_error = NULL,
                   updated_at = datetime('now', 'localtime')
               WHERE flight_key = ?
                 AND status = 'accepted_pending_reference'""",
            (reference, message, flight_key))
        return int(cursor.rowcount or 0)


def list_portal_jobs(limit: int = 10) -> list[dict]:
    limit = max(1, min(int(limit or 10), 50))
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM portal_jobs
               ORDER BY updated_at DESC, created_at DESC LIMIT ?""",
            (limit,)).fetchall()
    return [dict(row) for row in rows]


def set_override(flight_id: int, key: str, value):
    with connect() as conn:
        row = conn.execute("SELECT overrides FROM flights WHERE id = ?",
                           (flight_id,)).fetchone()
        if not row:
            return
        overrides = json.loads(row["overrides"] or "{}")
        if value in (None, ""):
            overrides.pop(key, None)
        else:
            overrides[key] = value
        conn.execute("UPDATE flights SET overrides = ? WHERE id = ?",
                     (json.dumps(overrides), flight_id))


def set_overrides(flight_id: int, values: dict):
    """Atomically update several user corrections for one flight."""
    with connect() as conn:
        row = conn.execute("SELECT overrides FROM flights WHERE id = ?",
                           (flight_id,)).fetchone()
        if not row:
            return False
        overrides = json.loads(row["overrides"] or "{}")
        for key, value in values.items():
            if value in (None, ""):
                overrides.pop(key, None)
            else:
                overrides[key] = value
        conn.execute("UPDATE flights SET overrides = ? WHERE id = ?",
                     (json.dumps(overrides), flight_id))
        return True


def reset():
    with connect() as conn:
        conn.execute("DELETE FROM flight_status_current")
        conn.execute("DELETE FROM flight_status_observations")
        conn.execute("DELETE FROM complaint_responses")
        conn.execute("DELETE FROM complaint_response_state")
        conn.execute("DELETE FROM gaca_status_checks")
        conn.execute("DELETE FROM gaca_account_cases")
        conn.execute("DELETE FROM gaca_account_sync")
        conn.execute("DELETE FROM flight_emails")
        conn.execute("DELETE FROM flights")
        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM mail_events")
        conn.execute("DELETE FROM mailbox_cursors")
        conn.execute("DELETE FROM complaints")
        conn.execute("DELETE FROM telegram_surveys")
        conn.execute("DELETE FROM telegram_events")
        conn.execute("DELETE FROM ai_profile_cache")
        conn.execute("DELETE FROM ai_analysis_cache")
