"""SQLite storage for parsed emails, linked flights and manual overrides."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH

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
        conn.executescript(_SCHEMA)


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
    return flight


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


def reset():
    with connect() as conn:
        conn.execute("DELETE FROM flight_emails")
        conn.execute("DELETE FROM flights")
        conn.execute("DELETE FROM emails")
