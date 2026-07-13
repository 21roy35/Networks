"""Glue: ingest raw emails -> parse -> store -> link -> flights table."""

import time
import threading
from datetime import datetime

from . import db
from .config import SAMPLE_EMAILS_DIR
from .linker import link_emails
from .mail_client import fetch_airline_emails, load_eml_files
from .parser import parse_email


MAILBOX_SCAN_LOCK = threading.Lock()


def ingest(raw_emails, log=print, progress: dict | None = None) -> int:
    """Parse and store raw email dicts; returns number of flight emails kept."""
    db.init_db()
    kept = 0
    for raw in raw_emails:
        db.save_mail_event(raw)
        parsed = parse_email(raw["message_id"], raw["subject"], raw["sender"],
                             raw["date"], raw["body"])
        if parsed is None:
            continue
        db.save_email(parsed)
        kept += 1
        if progress is not None:
            progress["kept"] = kept
        log(f"  + [{','.join(parsed.kinds) or 'other'}] {parsed.subject[:70]}")
    return kept


def rebuild_flights(log=print) -> int:
    """Re-link all stored emails into flight records."""
    db.init_db()
    flights = link_emails(db.all_emails(), log=log)
    db.replace_flights(flights)
    log(f"Linked into {len(flights)} flight(s).")
    return len(flights)


def reparse_emails(log=print) -> int:
    """Re-run extraction on every stored email body, then re-link.

    Lets parser improvements take effect without a slow IMAP rescan.
    Emails that the parser now recognises as marketing noise are removed.
    """
    db.init_db()
    kept = removed = 0
    for email in db.all_emails():
        date = None
        if email.get("date"):
            try:
                date = datetime.fromisoformat(email["date"])
            except ValueError:
                date = None
        parsed = parse_email(email["message_id"], email.get("subject") or "",
                             email.get("sender") or "", date,
                             email.get("body") or "")
        if parsed is None:
            db.delete_email(email["db_id"])
            removed += 1
        else:
            db.save_email(parsed)
            kept += 1
    log(f"Re-parsed {kept} email(s), removed {removed} as noise.")
    return rebuild_flights(log=log)


def scan_mailbox(config: dict, log=print, progress: dict | None = None) -> int:
    with MAILBOX_SCAN_LOCK:
        if progress is None:
            progress = {}
        progress.setdefault("started", time.time())
        kept = ingest(fetch_airline_emails(config, log=log, progress=progress),
                      log=log, progress=progress)
        log(f"Stored {kept} flight-related email(s).")
        progress["phase"] = "linking"
        flights = rebuild_flights(log=log)
        progress.update(phase="done", flights=flights, finished=time.time())
        return flights


def load_demo(log=print) -> int:
    kept = ingest(load_eml_files(SAMPLE_EMAILS_DIR, log=log), log=log)
    log(f"Stored {kept} flight-related email(s).")
    return rebuild_flights(log=log)
