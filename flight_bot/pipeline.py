"""Glue: ingest raw emails -> parse -> store -> link -> flights table."""

import time

from . import db
from .config import SAMPLE_EMAILS_DIR
from .linker import link_emails
from .mail_client import fetch_airline_emails, load_eml_files
from .parser import parse_email


def ingest(raw_emails, log=print, progress: dict | None = None) -> int:
    """Parse and store raw email dicts; returns number of flight emails kept."""
    db.init_db()
    kept = 0
    for raw in raw_emails:
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
    flights = link_emails(db.all_emails())
    db.replace_flights(flights)
    log(f"Linked into {len(flights)} flight(s).")
    return len(flights)


def scan_mailbox(config: dict, log=print, progress: dict | None = None) -> int:
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
