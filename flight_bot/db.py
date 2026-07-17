"""SQLite storage for parsed emails, linked flights and manual overrides."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH, passenger_profile_key

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

CREATE TABLE IF NOT EXISTS mailbox_cursors (
    folder TEXT PRIMARY KEY,
    uidvalidity TEXT NOT NULL,
    last_uid INTEGER NOT NULL,
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
    to_addr TEXT,
    subject TEXT,
    reference TEXT,
    details TEXT,
    attachments TEXT DEFAULT '[]',
    status TEXT NOT NULL,        -- 'sent' | 'filed'
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS complaint_responses (
    complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    mail_event_id INTEGER NOT NULL UNIQUE
        REFERENCES mail_events(id) ON DELETE CASCADE,
    match_method TEXT NOT NULL,  -- exact_reference | case_facts | fifo_airline
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

CREATE TABLE IF NOT EXISTS portal_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT,
    airline_code TEXT,
    flight_number TEXT,
    flight_key TEXT,
    status TEXT NOT NULL,
    message TEXT,
    reference TEXT,
    screenshot_file TEXT,
    terminal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
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

CREATE INDEX IF NOT EXISTS idx_mail_events_date
    ON mail_events(date, id);
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
CREATE INDEX IF NOT EXISTS idx_portal_jobs_updated
    ON portal_jobs(updated_at, id);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(complaints)")}
        if "reference" not in columns:
            conn.execute("ALTER TABLE complaints ADD COLUMN reference TEXT")
        if "details" not in columns:
            conn.execute("ALTER TABLE complaints ADD COLUMN details TEXT")
        if "attachments" not in columns:
            conn.execute(
                "ALTER TABLE complaints ADD COLUMN attachments TEXT DEFAULT '[]'")
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
                        last_uid: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO mailbox_cursors
                   (folder, uidvalidity, last_uid, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(folder) DO UPDATE SET
                   uidvalidity=excluded.uidvalidity,
                   last_uid=excluded.last_uid,
                   updated_at=excluded.updated_at""",
            (folder, str(uidvalidity), int(last_uid)))


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
                  attachments: list[str] | None = None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO complaints
               (flight_key, kind, to_addr, subject, status, reference, details,
                attachments)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (flight_key, kind, to_addr, subject, status, reference, details,
             json.dumps(attachments or [])))


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


def begin_complaint(flight_key: str, kind: str, subject: str | None,
                    details: str | None = None,
                    attachments: list[str] | None = None) -> int | None:
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
            stale = conn.execute(
                """SELECT 1 WHERE ? = 'filing'
                   AND datetime(?) < datetime('now', 'localtime', '-45 minutes')""",
                (current["status"], current["created_at"])).fetchone()
            if not stale:
                return None
            conn.execute(
                "UPDATE complaints SET status = 'interrupted' WHERE id = ?",
                (current["id"],))
        cursor = conn.execute(
            """INSERT INTO complaints
               (flight_key, kind, subject, status, details, attachments)
               VALUES (?, ?, ?, 'filing', ?, ?)""",
            (flight_key, kind, subject, details,
             json.dumps(attachments or [])))
        return int(cursor.lastrowid)


def finish_complaint(complaint_id: int, status: str,
                     reference: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE complaints SET status = ?,
               reference = COALESCE(?, reference) WHERE id = ?""",
            (status, reference, complaint_id))


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
    return results


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
    return results


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


def mark_event_seen(event_key: str):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO telegram_events (event_key) VALUES (?)",
            (event_key,))


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


def save_portal_job(job: dict) -> None:
    """Persist portal stages so restarts do not erase failure context."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO portal_jobs
                   (id, kind, airline_code, flight_number, flight_key,
                    status, message, reference, screenshot_file, terminal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   status=excluded.status,
                   message=excluded.message,
                   reference=excluded.reference,
                   screenshot_file=COALESCE(excluded.screenshot_file,
                                            portal_jobs.screenshot_file),
                   terminal=excluded.terminal,
                   updated_at=datetime('now', 'localtime')""",
            (str(job.get("id") or ""), str(job.get("kind") or ""),
             str(job.get("airline_code") or ""),
             str(job.get("flight_number") or ""),
             str(job.get("flight_key") or ""),
             str(job.get("status") or "queued"),
             str(job.get("message") or "")[:2000],
             str(job.get("reference") or ""),
             str(job.get("screenshot_file") or "") or None,
             int(bool(job.get("terminal")))))


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
        conn.execute("DELETE FROM complaint_responses")
        conn.execute("DELETE FROM complaint_response_state")
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
