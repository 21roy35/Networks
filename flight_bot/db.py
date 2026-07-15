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
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
        return {"values": {}, "evidence": {}}
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

    values, evidence = {}, {}
    for id_type in ("national_id", "iqama", "passport"):
        occurrences = national_candidates.get(id_type) or []
        distinct = {value.casefold(): value for value, _source in occurrences}
        if len(distinct) == 1:
            values["national_id"], evidence["national_id"] = occurrences[0]
            break
    immutable = {"alfursan_id", "title", "nationality"}
    for field, occurrences in candidates.items():
        distinct = {value.casefold(): value for value, _source in occurrences}
        if field in immutable and len(distinct) != 1:
            continue
        value, source = occurrences[0]
        values[field] = value
        evidence[field] = source
    return {"values": values, "evidence": evidence}


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
            """SELECT c.*, f.id AS flight_id, f.data AS flight_data
               FROM complaints c
               LEFT JOIN flights f ON f.flight_key = c.flight_key
               ORDER BY c.created_at DESC, c.id DESC""").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["attachments"] = json.loads(item.get("attachments") or "[]")
        item["flight_data"] = (json.loads(item["flight_data"])
                               if item.get("flight_data") else {})
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
        conn.execute("DELETE FROM flight_emails")
        conn.execute("DELETE FROM flights")
        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM mail_events")
        conn.execute("DELETE FROM complaints")
        conn.execute("DELETE FROM telegram_surveys")
        conn.execute("DELETE FROM telegram_events")
