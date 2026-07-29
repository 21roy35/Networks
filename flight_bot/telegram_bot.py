"""Telegram-first orchestration for post-flight feedback and complaints."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import db
from .ai_assistant import ClaudeAssistant
from .airlines import AIRLINES
from .captcha_solver import TwoCaptchaSolver
from .case_strategy import recommend_case
from .complaints import complaint_payload, missing_portal_fields
from .config import TELEGRAM_EVIDENCE_DIR, passenger_profile_key
from .flight_status import (get_flight_status, live_landed, parse_flight_time,
                            refresh_flight_status, schedule_has_finished)
from .gaca_account import sync_gaca_account
from .mail_client import fetch_recent_verification_message
from .pipeline import rebuild_flights, scan_mailbox
from .portal_automation import (PortalResult, _extract_reference, set_ai_handler,
                                set_captcha_solver, set_verification_handler,
                                resume_due_portal_jobs, start_portal_job)
from .ticket_import import (
    build_parsed_ticket,
    deterministic_ticket_details,
    explicit_manual_complaint_request,
    explicit_ticket_import_request,
    extract_pdf_text,
    manual_complaint_from_text,
    merge_ticket_details,
    missing_ticket_fields,
    normalize_ticket_details,
    selectors_from_text,
    ticket_preview,
)
from .web_access import create_web_token


logger = logging.getLogger(__name__)


def _verification_code_from_text(value: str) -> str:
    """Rank short numbers by proximity to verification language."""
    value = str(value or "")
    context_matches = list(re.finditer(
        r"otp|verification|one[ -]?time|passcode|security code|"
        r"رمز\s*(?:التحقق|التأكيد|الدخول)",
        value, re.I))
    ranked = []
    for match in re.finditer(r"(?<!\d)(\d{4,8})(?!\d)", value):
        distance = min((
            min(abs(match.start() - item.end()),
                abs(item.start() - match.end()))
            for item in context_matches
        ), default=1000)
        score = max(0, 200 - distance)
        score += 10 if len(match.group(1)) == 4 else 0
        ranked.append((score, match.group(1)))
    return max(ranked, default=(0, ""))[1]


def _buttons(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": data}
                                 for text, data in row] for row in rows]}


def _clean_excerpt(value: str, limit: int = 1300) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _airline_confirmation_reference(subject: str, body: str) -> str:
    """Extract case ids, including Saudia's real Ticket C_123456 format.

    Plain ticket numbers are accepted only from complaint-lifecycle subjects,
    so itinerary and e-ticket messages cannot be attached to a complaint.
    """
    blob = " ".join((subject or "", body or ""))
    # Prefer the carrier's explicit C_ reference before generic phrases such
    # as "Voucher no." or "Ticket number"; those may contain 13-digit
    # e-ticket/EMD identifiers that are not complaint references.
    explicit = re.search(
        r"(?<![A-Z0-9])C[_-](\d{6,})(?!\d)", blob, re.I)
    if explicit:
        return f"C_{explicit.group(1)}"
    reference = _extract_reference(blob)
    if reference and not (
        reference.isdigit() and len(reference) > 9
    ):
        return reference
    if not re.search(
            r"SAUDIA-Guest Relations|Registered with us|Under Investigation|"
            r"Your Ticket|Ticket\s+[A-Z]_\d+",
            subject or "", re.I):
        return ""
    match = re.search(
        r"(?:\[\s*)?(?:Your\s+)?Ticket\s*:?[\s\xa0]*"
        r"([A-Z]_\d{6,}|\d{6,})(?:\s*\])?",
        subject or "", re.I)
    return match.group(1).upper() if match else ""


def _telegram_sms_reference(value: str) -> str:
    """Return a Saudia case ref, not a passenger's 13-digit e-ticket."""
    match = re.search(r"(?<![A-Z0-9])C[\s_-]*(\d{6,})(?!\d)",
                      value or "", re.I)
    if match:
        return f"C_{match.group(1)}"
    # Some Saudia notifications say only "Your Ticket 2774567" even though
    # later correspondence renders the same case as C_2774567. Require clear
    # case context and 6-9 digits so a 13-digit passenger e-ticket is ignored.
    contextual = re.search(
        r"\b(?:reference|complaint|case|(?:service\s+)?ticket)\s*"
        r"(?:number|no\.?|id)?\s*(?:is\s*)?[:#-]?\s*(\d{6,9})\b",
        value or "", re.I)
    return f"C_{contextual.group(1)}" if contextual else ""


def _gaca_confirmation_reference(subject: str, body: str) -> str:
    """Accept only GACA's public C-number, never an internal detailsId."""
    blob = " ".join((subject or "", body or ""))
    match = re.search(r"(?<![A-Z0-9_])(C\d{6,})(?!\d)", blob, re.I)
    return match.group(1).upper() if match else ""


def _gaca_case_fact_score(blob: str, complaint: dict) -> int:
    flight = complaint.get("flight_data") or {}
    compact_blob = re.sub(r"[^a-z0-9]", "", str(blob or "").casefold())

    def compact(value) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

    score = 0
    pnr = compact(flight.get("pnr"))
    if len(pnr) >= 5 and pnr in compact_blob:
        score += 100
    tickets = list(flight.get("ticket_numbers") or [])
    if flight.get("ticket_number"):
        tickets.append(flight["ticket_number"])
    if any(
        len(compact(ticket)) >= 8 and compact(ticket) in compact_blob
        for ticket in tickets
    ):
        score += 100
    numbers = list(flight.get("flight_numbers") or [])
    if flight.get("flight_number"):
        numbers.append(flight["flight_number"])
    if any(
        len(compact(number)) >= 4 and compact(number) in compact_blob
        for number in numbers
    ):
        score += 45
    return score


def reconcile_gaca_mail_events(events: list[dict], notify=None) -> int:
    """Attach regulator email references to the exact emailed escalation."""
    pending = [
        complaint
        for complaint in db.list_complaints()
        if complaint.get("kind") == "gaca"
        and complaint.get("status") == "accepted_pending_reference"
        and not complaint.get("reference")
    ]
    reconciled = 0
    for event in events:
        marker = f"gaca-reference-captured:{event.get('id')}"
        if db.event_seen(marker):
            continue
        sender_domain = parseaddr(event.get("sender") or "")[1].rsplit(
            "@", 1)[-1].casefold()
        if not (
            sender_domain == "gaca.gov.sa"
            or sender_domain.endswith(".gaca.gov.sa")
        ):
            continue
        reference = _gaca_confirmation_reference(
            event.get("subject") or "", event.get("body") or "")
        if not reference:
            continue
        blob = " ".join((
            event.get("subject") or "",
            event.get("body") or "",
        ))
        scored = [
            (_gaca_case_fact_score(blob, complaint), complaint)
            for complaint in pending
        ]
        matches = [
            complaint for score, complaint in scored if score > 0
        ]
        if len(matches) == 1:
            complaint = matches[0]
        elif len(pending) == 1:
            complaint = pending[0]
        else:
            continue
        if not db.reconcile_portal_confirmation(
            int(complaint["id"]), reference
        ):
            continue
        db.mark_event_seen(marker)
        pending = [
            item for item in pending
            if int(item["id"]) != int(complaint["id"])
        ]
        reconciled += 1
        if notify:
            notify(
                "Captured the GACA complaint reference from its official "
                f"email: {reference}."
            )
    return reconciled


class TelegramAPI:
    def __init__(self, token: str, session=None):
        self.token = token
        self.session = session or requests.Session()
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method: str, data: dict | None = None, files=None,
             timeout: int = 35):
        try:
            response = self.session.post(
                self.base + method, data=data or {}, files=files,
                timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Telegram API {method} is unavailable ({type(exc).__name__})."
            ) from None
        if response.status_code >= 400:
            raise RuntimeError(
                f"Telegram API {method} returned HTTP {response.status_code}.")
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError(
                f"Telegram API {method} returned invalid data.") from None
        if not result.get("ok"):
            description = str(result.get("description") or "request failed")
            raise RuntimeError(f"Telegram API {method}: {description[:300]}")
        return result.get("result")

    def updates(self, offset: int, timeout: int) -> list[dict]:
        return self.call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }, timeout=timeout + 10) or []

    def send_message(self, chat_id, text: str, reply_markup: dict | None = None,
                     force_reply: bool = False) -> dict:
        markup = reply_markup
        if force_reply:
            markup = {"force_reply": True, "selective": True,
                      "input_field_placeholder": "Reply to FlightDeck"}
        data = {"chat_id": chat_id, "text": text[:4096]}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        return self.call("sendMessage", data)

    def send_photo(self, chat_id, image: bytes, caption: str,
                   reply_markup: dict | None = None) -> dict:
        data = {"chat_id": chat_id, "caption": caption[:1024]}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return self.call(
            "sendPhoto", data,
            files={"photo": ("verification.png", image, "image/png")})

    def answer_callback(self, query_id: str, text: str = ""):
        self.call("answerCallbackQuery", {
            "callback_query_id": query_id, "text": text[:200]})

    def set_commands(self):
        commands = [
            {"command": "status", "description": "Show FlightDeck status"},
            {"command": "ticket", "description": "Add a ticket or itinerary"},
            {"command": "complaintref",
             "description": "Attach a manually filed airline case"},
            {"command": "flightstatus", "description": "Refresh a flight status"},
            {"command": "recommend", "description": "Best complaint next step"},
            {"command": "gaca", "description": "Sync your GACA complaint account"},
            {"command": "web", "description": "Open the private dashboard"},
            {"command": "cancel", "description": "Cancel pending issue intake"},
        ]
        self.call("setMyCommands", {"commands": json.dumps(commands)})

    def delete_message(self, chat_id, message_id: int):
        try:
            self.call("deleteMessage", {
                "chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass

    def download(self, file_id: str, destination: Path):
        record = self.call("getFile", {"file_id": file_id})
        response = self.session.get(
            self.file_base + record["file_path"], timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Telegram file download returned HTTP {response.status_code}.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)


@dataclass
class VerificationWaiter:
    kind: str
    event: threading.Event = field(default_factory=threading.Event)
    response: object = None


@dataclass
class PendingIntake:
    flight_key: str
    incident: str = ""
    attachments: list[str] = field(default_factory=list)
    parent_complaint_id: int | None = None
    timer: threading.Timer | None = None


class TelegramCoordinator:
    def __init__(self, config: dict, api: TelegramAPI | None = None):
        self.config = config
        self.settings = config.get("telegram") or {}
        self.chat_id = str(self.settings.get("chat_id") or "")
        self.api = api or TelegramAPI(self.settings.get("bot_token") or "")
        self.ai = ClaudeAssistant(config)
        self.captcha = TwoCaptchaSolver(config)
        self.stop_event = threading.Event()
        self.started_at = datetime.now()
        self.offset = 0
        self._lock = threading.RLock()
        self._verification: VerificationWaiter | None = None
        self._intakes: dict[str, PendingIntake] = {}
        self._last_mail_scan = 0.0
        self._mail_scan_thread: threading.Thread | None = None
        self._mail_scan_error = ""
        self._status_cache: dict[str, tuple[float, bool | None]] = {}
        self._used_verification_emails: set[str] = set()
        self._ai_chat_lock = threading.Lock()
        self._ai_chat_thread: threading.Thread | None = None
        self._ai_context: dict[str, object] = {}
        self._gaca_sync_lock = threading.Lock()
        self._gaca_sync_thread: threading.Thread | None = None
        self._last_gaca_sync = 0.0
        # Legacy floor kept for migrations/tests; FIFO response matching is disabled.
        self._fifo_response_floor = db.initialize_fifo_response_floor()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled")
                    and self.settings.get("bot_token") and self.chat_id)

    def start(self):
        if not self.enabled:
            return self
        db.init_db()
        TELEGRAM_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        set_verification_handler(self.request_verification)
        set_ai_handler(
            self.ai.portal_decision if self.ai.enabled else None,
            int(self.ai.settings.get("max_portal_attempts", 3)))
        set_captcha_solver(
            self.captcha.solve if self.captcha.enabled else None)
        recovered = db.recover_interrupted_portal_jobs()
        if recovered["retry_wait"] or recovered["quarantined"]:
            self.notify(
                "FlightDeck recovered interrupted portal work: "
                f"{recovered['retry_wait']} pre-submit job(s) were safely "
                f"requeued and {recovered['quarantined']} Submit-stage job(s) "
                "were quarantined for reference reconciliation.")
        resume_due_portal_jobs(self.portal_progress_handler)
        threading.Thread(
            target=self._register_commands, name="telegram-commands",
            daemon=True).start()
        threading.Thread(target=self._poll_loop, name="telegram-updates",
                         daemon=True).start()
        threading.Thread(
            target=self._portal_resume_loop,
            name="portal-job-resume",
            daemon=True,
        ).start()
        threading.Thread(target=self._monitor_loop, name="telegram-monitor",
                         daemon=True).start()
        return self

    def _register_commands(self):
        """Register Telegram commands without delaying web-server startup."""
        try:
            self.api.set_commands()
        except Exception:
            # Command-menu registration is convenient but must never prevent
            # polling, monitoring, or complaint filing from starting.
            logger.exception("Telegram command registration failed")

    def stop(self):
        self.stop_event.set()
        set_verification_handler(None)
        set_ai_handler(None)
        set_captcha_solver(None)

    def notify(self, text: str, buttons=None, force_reply: bool = False) -> dict:
        try:
            result = self.api.send_message(
                self.chat_id, text, reply_markup=buttons,
                force_reply=force_reply)
        except Exception as exc:
            # A temporary Telegram outage must not abort an already-safe
            # provider/regulator workflow before the official portal opens.
            logger.warning(
                "Telegram notification could not be delivered: %s", exc)
            result = {}
        try:
            db.record_telegram_message(
                "outgoing", result.get("message_id") if result else None,
                text, "text")
        except Exception:
            logger.exception("Could not journal outgoing Telegram message")
        return result

    def _send_photo(self, image: bytes, caption: str,
                    buttons: dict | None = None) -> dict:
        try:
            result = self.api.send_photo(
                self.chat_id, image, caption, reply_markup=buttons)
        except Exception as exc:
            logger.warning(
                "Telegram screenshot could not be delivered: %s", exc)
            result = {}
        try:
            db.record_telegram_message(
                "outgoing", result.get("message_id") if result else None,
                caption, "photo")
        except Exception:
            logger.exception("Could not journal outgoing Telegram photo")
        return result

    def portal_progress_handler(self):
        """Return a per-job Telegram relay with readable stage transitions."""
        state = {"stage": "", "key": None}
        deliveries: queue.Queue = queue.Queue()
        worker_started = threading.Event()
        labels = {
            "queued": "preparing the portal",
            "opening": "opening the official website",
            "filling": "filling the complaint form",
            "reviewing": "checking the completed form",
            "verification": "completing verification",
            "submitting": "submitting the complaint",
            "submitted": "complaint submitted",
            "accepted_pending_reference": "waiting for the airline reference",
            "confirmation_unknown": "checking the airline confirmation",
            "needs_attention": "waiting for your attention",
            "error": "stopped with an error",
            "retry_wait": "queued for a safe retry",
            "quarantined": "quarantined pending reconciliation",
        }
        terminal = {
            "submitted", "accepted_pending_reference", "confirmation_unknown",
            "needs_attention", "error", "retry_wait", "quarantined",
        }

        def deliver():
            while True:
                status, text, image = deliveries.get()
                try:
                    if image:
                        self._send_photo(image, text)
                    else:
                        self.notify(text)
                except Exception:
                    logger.exception("Telegram portal progress delivery failed")
                finally:
                    deliveries.task_done()
                if status in terminal:
                    return

        def relay(status: str, message: str,
                  image: bytes | None = None) -> None:
            key = (status, message)
            previous = state["stage"]
            if (key == state["key"]
                    or (status in terminal and previous == status)):
                return
            current_label = labels.get(status, status.replace("_", " "))
            if status == "submitted":
                heading = "✅ Finished: submitting the complaint\n✅ Result: complaint submitted"
            elif status in {"needs_attention", "error"}:
                heading = f"⚠️ Stage: {current_label}"
            elif previous and previous != status:
                previous_label = labels.get(
                    previous, previous.replace("_", " "))
                heading = (f"✅ Finished: {previous_label}\n"
                           f"⏳ Doing now: {current_label}")
            else:
                heading = f"⏳ Doing now: {current_label}"
            text = f"{heading}\n{message}"[:1000]
            state.update(stage=status, key=key)
            if not worker_started.is_set():
                worker_started.set()
                threading.Thread(
                    target=deliver, name="portal-progress", daemon=True).start()
            deliveries.put((status, text, image))

        return relay

    def request_verification(self, challenge: dict):
        waiter = VerificationWaiter(challenge.get("kind") or "verification")
        with self._lock:
            if self._verification:
                return None
            self._verification = waiter
        choices = challenge.get("choices") or []
        markup = None
        if choices:
            markup = _buttons([[(choice, f"verify:{choice.lower()}")
                                for choice in choices]])
        prompt = "🔐 FlightDeck verification\n\n" + challenge.get("message", "")
        image = challenge.get("image") or b""
        if image:
            self._send_photo(image, prompt, buttons=markup)
        else:
            self.notify(prompt, buttons=markup, force_reply=not choices)
        timeout = int(self.settings.get("verification_timeout_minutes", 10)) * 60
        started = datetime.now().astimezone()
        deadline = time.monotonic() + timeout
        next_email_check = 0.0
        while not waiter.event.is_set() and time.monotonic() < deadline:
            if waiter.kind == "otp" and time.monotonic() >= next_email_check:
                next_email_check = time.monotonic() + 8
                try:
                    message = fetch_recent_verification_message(
                        self.config,
                        since=started,
                        recipient=str(
                            challenge.get("recipient_email") or "").strip(),
                    )
                    message_id = str(
                        (message or {}).get("message_id") or "")
                    if (message and message_id
                            and message_id in self._used_verification_emails):
                        message = None
                    if message:
                        blob = "\n".join((
                            str(message.get("subject") or ""),
                            str(message.get("body") or ""),
                        ))
                        code = _verification_code_from_text(blob)
                        if not code and self.ai.enabled:
                            distilled = self.ai.distill_sms(
                                str(message.get("sender") or "email"), blob)
                            code = re.sub(
                                r"\D", "",
                                str((distilled or {}).get("otp") or ""))
                        if code and self.accept_verification_code(
                                code, source="email"):
                            if message_id:
                                self._used_verification_emails.add(message_id)
                            self.notify(
                                "FlightDeck received the current portal code "
                                "from email and entered it automatically.")
                except Exception:
                    logger.exception(
                        "Automatic verification-email check failed")
            waiter.event.wait(min(1.0, max(0.0, deadline - time.monotonic())))
        with self._lock:
            if self._verification is waiter:
                self._verification = None
        if not waiter.event.is_set():
            self.notify("Verification timed out. The portal submission was paused safely.")
            return None
        return waiter.response

    def accept_verification_code(self, code: str,
                                 *, source: str = "shortcut") -> bool:
        """Deliver a trusted short-lived code to the active portal waiter."""
        code = re.sub(r"\D", "", str(code or ""))
        if not re.fullmatch(r"\d{4,8}", code):
            return False
        with self._lock:
            waiter = self._verification
            if not waiter or waiter.kind != "otp" or waiter.event.is_set():
                return False
            waiter.response = code
            waiter.event.set()
        logger.info("Portal OTP supplied from %s without storing its body", source)
        return True

    def _poll_loop(self):
        timeout = int(self.settings.get("poll_timeout_seconds", 25))
        consecutive_failures = 0
        while not self.stop_event.is_set():
            try:
                for update in self.api.updates(self.offset, timeout):
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    self.handle_update(update)
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                # Telegram long polling occasionally times out or drops a
                # connection. Updates remain queued at the saved offset, so use
                # a short bounded backoff without flooding the error log.
                if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                    logger.warning(
                        "Telegram polling temporarily unavailable (attempt %s): %s",
                        consecutive_failures, exc)
                self.stop_event.wait(min(30, 2 ** min(consecutive_failures, 5)))

    def _portal_resume_loop(self):
        """Lease due portal jobs independently of slower mailbox sweeps."""
        while not self.stop_event.is_set():
            try:
                resume_due_portal_jobs(self.portal_progress_handler, limit=1)
            except Exception:
                logger.exception("Dedicated portal resume cycle failed")
            self.stop_event.wait(15)

    def _monitor_loop(self):
        interval = max(15, int(self.settings.get("monitor_interval_seconds", 60)))
        while not self.stop_event.is_set():
            try:
                self._maybe_scan_mailbox()
                self.refresh_watched_flights()
                self.send_due_surveys()
                self.check_complaint_responses()
                self.ask_for_pending_references()
                self.resume_pending_parent_escalations()
                self.auto_escalate_due_complaints()
                self._maybe_sync_gaca_account()
                # The dedicated resume loop and this slower monitor loop both
                # use a one-job lease.  The portal worker's global browser
                # lock then guarantees that only one official-site request
                # workflow can run at a time.
                resume_due_portal_jobs(
                    self.portal_progress_handler, limit=1)
            except Exception:
                logger.exception("Telegram monitor cycle failed")
            self.stop_event.wait(interval)

    def _authorized(self, chat_id) -> bool:
        return str(chat_id) == self.chat_id

    def handle_update(self, update: dict):
        if callback := update.get("callback_query"):
            message = callback.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            if self._authorized(chat_id):
                self._handle_callback(callback)
            return
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if self._authorized(chat_id):
            self._handle_message(message)

    def _handle_verification_message(self, message: dict) -> bool:
        with self._lock:
            waiter = self._verification
        if not waiter:
            return False
        response = message.get("text") or message.get("caption") or ""
        if not response.strip():
            return True
        safe_labels = {
            "otp": "[OTP response received]",
            "recaptcha": "[CAPTCHA response received]",
            "hcaptcha": "[CAPTCHA response received]",
            "captcha": "[CAPTCHA response received]",
            "text_captcha": "[CAPTCHA response received]",
            "field_input": "[Required field response received]",
            "login": "[Portal login response received]",
            "approval": "[Portal approval response received]",
        }
        try:
            db.record_telegram_message(
                "incoming", message.get("message_id"),
                safe_labels.get(waiter.kind, "[Verification response received]"),
                "photo" if message.get("photo") else "text",
                (message.get("reply_to_message") or {}).get("message_id"))
        except Exception:
            logger.exception("Could not journal Telegram verification response")
        waiter.response = response.strip()
        waiter.event.set()
        if waiter.kind == "otp":
            self.api.delete_message(self.chat_id, message["message_id"])
        return True

    @staticmethod
    def _looks_like_survey_issue(value: str) -> bool:
        """Separate a flight-issue answer from an unrelated bot question."""
        value = " ".join(str(value or "").split()).strip()
        if not value:
            return False
        if re.match(
                r"^(?:show|list|find|send|check|sync|open|what|which|when|"
                r"where|who|how|is|are|did|do|does|can|could|tell me)\b",
                value, re.IGNORECASE):
            return False
        return bool(re.search(
            r"\b(?:broken|broke|damag\w*|delay\w*|cancel\w*|lost|missing|"
            r"didn['’]?t work|doesn['’]?t work|not working|failed|"
            r"unavailable|rude|bad service|seat|screen|baggage|bag|luggage|"
            r"meal|wheelchair|refund|complain|issue|problem)\b|"
            r"(?:مكسور|تالف|تعطل|تأخر|ضاعت|حقيبة|شنطة|مشكلة)",
            value, re.IGNORECASE))

    @staticmethod
    def _looks_like_bot_or_portal_query(value: str) -> bool:
        """Keep operational questions out of an unrelated flight intake."""
        value = " ".join(str(value or "").split()).strip()
        if not value:
            return False
        return bool(re.search(
            r"\b(?:captcha|2captcha|portal|screenshot|screen\s*shot|bot|"
            r"telegram|submission|submit\s+button|job|stage|logs?|"
            r"automation)\b|\b(?:why|how|what)\b.{0,80}\b(?:fail|error|"
            r"work|happen|understand)\w*\b|\b(?:fail|error)\w*\b.{0,80}"
            r"\b(?:complaint|submit|portal|bot)\b",
            value, re.IGNORECASE))

    def _handle_message(self, message: dict):
        if self._handle_verification_message(message):
            return
        text = (message.get("text") or "").strip()
        pasted = (message.get("text") or message.get("caption") or "").strip()
        reference_for_journal = _telegram_sms_reference(pasted)
        journal_text = (f"[Airline reference received: {reference_for_journal}]"
                        if reference_for_journal else pasted)
        try:
            db.record_telegram_message(
                "incoming", message.get("message_id"), journal_text,
                ("photo" if message.get("photo") else
                 "document" if message.get("document") else "text"),
                (message.get("reply_to_message") or {}).get("message_id"))
        except Exception:
            logger.exception("Could not journal incoming Telegram message")
        if text == "/start":
            self.notify(
                "FlightDeck Telegram is connected. Send /ticket with a PDF, "
                "photo, or booking details to add a flight; use /complaintref "
                "for a case you filed manually. I’ll check in after flights, "
                "collect issue photos, file official complaints, relay "
                "verification steps, and report airline responses. Use "
                "/status for service status or /web for your private dashboard.")
            return
        if text == "/status":
            self._send_status()
            return
        if text and text.split(maxsplit=1)[0].lower() == "/flightstatus":
            query = text.partition(" ")[2].strip()
            self._send_live_status({"query": query, "latest": not query}, force=True)
            return
        if text and text.split(maxsplit=1)[0].lower() == "/recommend":
            query = text.partition(" ")[2].strip()
            self._send_case_recommendation(
                {"query": query, "latest": not query}, question=text)
            return
        if text and text.split(maxsplit=1)[0].lower() == "/gaca":
            self.start_gaca_account_sync(manual=True)
            return
        if text and text.split(maxsplit=1)[0].lower() == "/web":
            self._send_web_link()
            return
        if text == "/cancel":
            pending_import = db.latest_pending_ticket_import(self.chat_id)
            if pending_import:
                db.update_ticket_import(
                    pending_import["token"], status="cancelled")
            self._cancel_latest_intake()
            self.notify("Cancelled the pending ticket or complaint intake.")
            return

        positive = re.fullmatch(
            r"(?:good|great|fine|perfect|all good|no issues?|it was good|"
            r"ممتاز|جيد|تمام|ما فيه مشاكل)[.! ]*", text, re.I)
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        survey = (db.pending_survey(self.chat_id, reply_id)
                  if reply_id is not None else None)
        if (not survey and pasted and self.ai.enabled
                and self._looks_like_bot_or_portal_query(pasted)):
            self._dispatch_ai_message(pasted)
            return
        if not survey:
            pending = db.pending_survey(self.chat_id)
            if (pending and (
                    pending.get("status") in {"awaiting_details", "collecting"}
                    or positive or message.get("photo")
                    or self._looks_like_survey_issue(pasted))):
                survey = pending
        if not survey:
            command = text.split(maxsplit=1)[0].lower() if text else ""
            if (command == "/ticket"
                    and not text.partition(" ")[2].strip()
                    and not message.get("photo")
                    and not message.get("document")):
                self.notify(
                    "Send the ticket PDF/photo, or use:\n"
                    "/ticket Flight SV123, 2026-08-01, RUH to JED, "
                    "passenger Full Name, PNR ABC123, ticket 065-1234567890\n\n"
                    "You can include: manual complaint C_1234567 filed "
                    "2026-08-02 about <what happened>.",
                    force_reply=True)
                return
            if (command == "/complaintref"
                    and not text.partition(" ")[2].strip()):
                self.notify(
                    "Use: /complaintref C_1234567 flight SV123 "
                    "date 2026-08-01 filed 2026-08-02 about <what happened>.\n"
                    "I need enough flight detail to avoid attaching it to the "
                    "wrong family passenger.",
                    force_reply=True)
                return
            attachment = self._ticket_attachment(message)
            if (explicit_ticket_import_request(pasted)
                    or attachment and (
                        attachment.get("kind") == "document" or not pasted)):
                self._start_ticket_import(message)
                return
            if (command == "/complaintref"
                    and explicit_manual_complaint_request(pasted)):
                self._handle_manual_complaint_message(pasted)
                return
            if self._continue_ticket_import(message):
                return
            if explicit_manual_complaint_request(pasted):
                self._handle_manual_complaint_message(pasted)
                return

            reference = _telegram_sms_reference(pasted)
            if reference and self._capture_telegram_reference(
                    reference, pasted, message):
                return
        if not survey:
            if pasted and self.ai.enabled:
                self._dispatch_ai_message(pasted)
            elif not pasted and (message.get("photo") or message.get("document")):
                self.notify(
                    "I could not read that as a ticket. Send it again with "
                    "a caption such as “add this ticket”, or paste the flight "
                    "number, date, route, passenger, and PNR/e-ticket number.")
            else:
                self.notify(
                    "Reply to a post-flight question, use /ticket to add a "
                    "booking, /complaintref to attach a manual airline case, "
                    "or ask me about a flight, passenger, complaint, email, "
                    "or screenshot.")
            return
        if (survey.get("status") == "asked" and positive
                and not message.get("photo")):
            db.update_survey_status(survey["flight_key"], "good")
            flight = db.get_flight_by_key(survey["flight_key"])
            if flight and bool(self._effective_flight_value(
                    flight, "cancelled")):
                self.notify(
                    "Understood—I will not open a complaint for this cancellation.")
            else:
                self.notify("Glad the flight went well ✈️")
            return
        self._collect_issue(survey, message)

    @staticmethod
    def _ticket_attachment(message: dict) -> dict | None:
        photos = message.get("photo") or []
        if photos:
            return {
                "file_id": photos[-1].get("file_id") or "",
                "suffix": ".jpg",
                "media_type": "image/jpeg",
                "kind": "photo",
                "name": "telegram-ticket.jpg",
            }
        document = message.get("document") or {}
        if not document:
            return None
        mime_type = str(document.get("mime_type") or "").casefold()
        file_name = Path(str(
            document.get("file_name") or "telegram-ticket"
        )).name
        suffix = Path(file_name).suffix.lower()
        if mime_type == "application/pdf" or suffix == ".pdf":
            suffix, mime_type = ".pdf", "application/pdf"
        elif mime_type.startswith("image/") or suffix in {
                ".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = suffix if suffix in {
                ".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
            mime_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix, mime_type or "image/jpeg")
        elif mime_type.startswith("text/") or suffix in {".txt", ".csv"}:
            suffix = suffix if suffix in {".txt", ".csv"} else ".txt"
            mime_type = mime_type or "text/plain"
        else:
            return None
        return {
            "file_id": document.get("file_id") or "",
            "suffix": suffix,
            "media_type": mime_type,
            "kind": "document",
            "name": file_name,
        }

    def _start_ticket_import(self, message: dict) -> None:
        """Download/read a Telegram ticket without blocking update polling."""
        message_id = int(message.get("message_id") or 0)
        if not message_id:
            self.notify("I could not identify that Telegram message. Send it again.")
            return
        attachment = self._ticket_attachment(message)
        pasted = (message.get("text") or message.get("caption") or "").strip()
        token = uuid.uuid4().hex[:16]
        draft = db.create_ticket_import(
            token,
            self.chat_id,
            message_id,
            (attachment or {}).get("kind") or "text",
            source_text=pasted,
        )
        if draft.get("token") != token:
            status = draft.get("status")
            if status == "imported":
                self.notify("That ticket message is already on the dashboard.")
            else:
                self.notify("I am already reading that ticket message.")
            return
        self.notify(
            f"Reading the ticket now. {self.ai.name} will only fill fields "
            "that are visibly present; I’ll show you a preview before saving.")
        threading.Thread(
            target=self._process_ticket_import,
            args=(token, pasted, attachment),
            name=f"ticket-import-{token[:8]}",
            daemon=True,
        ).start()

    def _read_ticket_attachment(
            self, token: str, attachment: dict | None
    ) -> tuple[str, bytes | None, str, str]:
        if not attachment:
            return "", None, "", ""
        destination = (
            TELEGRAM_EVIDENCE_DIR / "ticket_imports"
            / f"{token}{attachment['suffix']}")
        self.api.download(attachment["file_id"], destination)
        mime_type = str(attachment.get("media_type") or "")
        if mime_type == "application/pdf":
            return extract_pdf_text(destination), None, mime_type, str(destination)
        if mime_type.startswith("text/"):
            return (
                destination.read_text(encoding="utf-8", errors="replace")[:60_000],
                None, mime_type, str(destination))
        return "", destination.read_bytes(), mime_type, str(destination)

    def _process_ticket_import(
            self, token: str, caption: str,
            attachment: dict | None = None) -> None:
        try:
            attachment_text, image, media_type, source_file = (
                self._read_ticket_attachment(token, attachment))
            source_text = "\n\n".join(filter(None, (
                caption, attachment_text)))[:60_000]
            deterministic = deterministic_ticket_details(source_text)
            details = deterministic
            needs_ai = bool(image) or bool(missing_ticket_fields(deterministic))
            if self.ai.enabled and needs_ai:
                with self._ai_chat_lock:
                    ai_details = self.ai.extract_ticket_details(
                        source_text,
                        image=image,
                        media_type=media_type or "image/jpeg",
                    )
                details = merge_ticket_details(deterministic, ai_details)
            missing = missing_ticket_fields(details)
            status = "awaiting_details" if missing else "ready"
            db.update_ticket_import(
                token,
                status=status,
                source_file=source_file,
                source_text=source_text,
                extracted=details,
                error="",
            )
            self._send_ticket_preview(token, details)
        except Exception as exc:
            logger.exception(
                "Telegram ticket import could not be read (%s)",
                type(exc).__name__)
            db.update_ticket_import(
                token,
                status="awaiting_details",
                error=f"{type(exc).__name__}: ticket could not be read",
            )
            self.notify(
                "I could not read that attachment reliably. Paste the flight "
                "number, date, route, passenger, and PNR/e-ticket number in "
                "one reply and I’ll continue the same import.",
                force_reply=True)

    def _send_ticket_preview(self, token: str, details: dict) -> None:
        missing = missing_ticket_fields(details)
        rows = [[("Cancel", f"ticket_cancel:{token}")]]
        if not missing:
            rows[0].insert(0, ("Add to dashboard", f"ticket_confirm:{token}"))
        prompt = ticket_preview(details)
        if missing:
            prompt += (
                "\n\nReply with the missing details in plain language. "
                "Nothing has been added yet.")
        else:
            prompt += (
                "\n\nConfirm to add this as durable Telegram ticket evidence. "
                "Nothing is filed with an airline by this button.")
        self.notify(prompt, buttons=_buttons(rows), force_reply=bool(missing))

    @staticmethod
    def _looks_like_ticket_followup(text: str) -> bool:
        return bool(re.search(
            r"\b(?:flight|date|route|from|to|origin|destination|passenger|"
            r"travell?er|pnr|booking|e-?ticket|ticket\s+(?:number|no)|"
            r"seat|class|national\s+id|alfursan|complaint|case|reference|"
            r"filed|submitted)\b",
            str(text or ""), re.IGNORECASE))

    def _continue_ticket_import(self, message: dict) -> bool:
        pending = db.latest_pending_ticket_import(self.chat_id)
        if not pending or pending.get("status") != "awaiting_details":
            return False
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text or not self._looks_like_ticket_followup(text):
            return False
        db.update_ticket_import(pending["token"], status="processing")
        self.notify("Adding those details to the ticket preview now.")
        threading.Thread(
            target=self._apply_ticket_followup,
            args=(pending["token"], text),
            name=f"ticket-followup-{pending['token'][:8]}",
            daemon=True,
        ).start()
        return True

    def _apply_ticket_followup(self, token: str, text: str) -> None:
        draft = db.get_ticket_import(token)
        if not draft:
            return
        try:
            deterministic = deterministic_ticket_details(text)
            followup = deterministic
            if self.ai.enabled and missing_ticket_fields(
                    merge_ticket_details(draft.get("extracted"), deterministic)):
                with self._ai_chat_lock:
                    ai_details = self.ai.extract_ticket_details(text)
                followup = merge_ticket_details(deterministic, ai_details)
            details = merge_ticket_details(draft.get("extracted"), followup)
            source_text = "\n\n".join(filter(None, (
                draft.get("source_text") or "", text)))[:60_000]
            status = (
                "awaiting_details" if missing_ticket_fields(details) else "ready")
            db.update_ticket_import(
                token,
                status=status,
                source_text=source_text,
                extracted=details,
                error="",
            )
            self._send_ticket_preview(token, details)
        except Exception:
            logger.exception("Ticket follow-up could not be applied")
            db.update_ticket_import(token, status="awaiting_details")
            self.notify(
                "I could not apply that safely. Please send the missing fields "
                "with explicit labels, for example “Passenger: …, PNR: …”.",
                force_reply=True)

    def _flights_for_source_message(self, message_id: str) -> list[dict]:
        matches = []
        for summary in db.list_flights():
            flight = db.get_flight(int(summary["id"]))
            if flight and any(
                    str(email.get("message_id") or "") == str(message_id)
                    for email in flight.get("emails") or []):
                matches.append(flight)
        return matches

    def _confirm_ticket_import(self, token: str) -> None:
        draft = db.get_ticket_import(token)
        if not draft or str(draft.get("chat_id")) != self.chat_id:
            self.notify("That ticket preview has expired.")
            return
        if draft.get("status") == "imported":
            self.notify("That ticket is already on the dashboard.")
            return
        details = normalize_ticket_details(draft.get("extracted"))
        missing = missing_ticket_fields(details)
        if missing:
            db.update_ticket_import(token, status="awaiting_details")
            self._send_ticket_preview(token, details)
            return
        db.update_ticket_import(token, status="importing")
        message_id = f"telegram-ticket:{self.chat_id}:{token}"
        imported_at = datetime.now()
        parsed = build_parsed_ticket(
            details,
            message_id=message_id,
            imported_at=imported_at,
            source_text=draft.get("source_text") or "",
            source_file=draft.get("source_file") or "",
        )
        db.save_mail_event({
            "message_id": message_id,
            "subject": parsed.subject,
            "sender": parsed.sender,
            "date": imported_at,
            "body": parsed.body_text,
        })
        db.save_email(parsed)
        rebuild_flights(log=logger.info)
        flights = self._flights_for_source_message(message_id)
        if not flights:
            db.update_ticket_import(
                token,
                status="awaiting_details",
                error="ticket source did not link to a flight",
            )
            self.notify(
                "The source was saved, but it did not produce a reliable flight "
                "record. I did not attach any complaint. Send the flight number "
                "and date again so I can repair the preview.")
            return
        keys = [flight["flight_key"] for flight in flights]
        db.update_ticket_import(
            token,
            status="imported",
            imported_flight_keys=keys,
            error="",
        )
        labels = ", ".join(self._flight_label(flight) for flight in flights)
        self.notify(
            f"✅ Added to the dashboard: {labels}. Flight monitoring and the "
            "normal post-flight check-in now apply to the imported passenger.")
        complaint = details.get("complaint") or {}
        if complaint.get("reference"):
            target_number = complaint.get("flight_number")
            targets = [
                flight for flight in flights
                if not target_number or target_number in {
                    flight.get("flight_number"),
                    *(flight.get("flight_numbers") or []),
                }]
            if len(targets) == 1:
                self._store_manual_complaint(targets[0], complaint)
            else:
                self.notify(
                    "The ticket was added, but I did not guess which leg owns "
                    f"manual complaint {complaint['reference']}. Send "
                    f"“/complaintref {complaint['reference']} flight <number> "
                    "filed <date>” to attach it exactly.")

    def _matching_manual_reference_flights(self, text: str) -> list[dict]:
        selectors = selectors_from_text(text)
        if not any(selectors.values()):
            return []
        flights = db.list_flights()
        if selectors.get("flight_number"):
            number = selectors["flight_number"]
            flights = [
                flight for flight in flights if number in {
                    flight.get("flight_number"),
                    *(flight.get("flight_numbers") or []),
                }]
        if selectors.get("pnr"):
            flights = [
                flight for flight in flights
                if str(flight.get("pnr") or "").upper() == selectors["pnr"]]
        if selectors.get("flight_date"):
            flights = [
                flight for flight in flights
                if str(flight.get("flight_date") or "")[:10]
                == selectors["flight_date"]]
        return flights

    def _handle_manual_complaint_message(self, text: str) -> None:
        complaint = manual_complaint_from_text(text)
        if not complaint.get("reference"):
            self.notify(
                "I found the manual-complaint request, but not a clear case "
                "number. Send “/complaintref <number> flight <number> "
                "date <flight date>”.")
            return
        flights = self._matching_manual_reference_flights(text)
        if len(flights) != 1:
            if flights:
                choices = "\n".join(
                    f"• {self._flight_summary(flight)}"
                    for flight in flights[:6])
                self.notify(
                    "I found more than one possible family flight and attached "
                    "nothing. Add the exact flight date or PNR:\n" + choices)
            else:
                self.notify(
                    "I could not match that reference to exactly one stored "
                    "flight. Include the flight number plus its flight date or "
                    "PNR; I will not guess between family passengers.")
            return
        self._store_manual_complaint(flights[0], complaint)

    def _store_manual_complaint(self, flight: dict, complaint: dict) -> None:
        try:
            saved, outcome = db.record_manual_airline_complaint(
                flight["flight_key"],
                complaint.get("reference") or "",
                details=complaint.get("text") or "",
                category=complaint.get("category") or "",
                filed_at=complaint.get("filed_at") or None,
            )
        except ValueError as exc:
            self.notify(str(exc))
            return
        created = parse_flight_time(saved.get("created_at"))
        delay_days = max(1, int(self.settings.get(
            "gaca_auto_escalate_days", 7)))
        due = created + timedelta(days=delay_days) if created else None
        due_text = due.strftime("%Y-%m-%d %H:%M") if due else "in seven days"
        if outcome == "completed_existing":
            source_text = (
                "This completed the reference that FlightDeck was already "
                "waiting for; it was not recorded as a second manual filing.")
        elif outcome == "already_linked":
            source_text = "It was already linked, so nothing was duplicated."
        else:
            source_text = (
                "It is marked as manually filed through Telegram, not as a "
                "complaint submitted by FlightDeck.")
        if not complaint.get("text"):
            source_text += (
                " You did not supply the original complaint text, so the "
                "dashboard says that explicitly instead of inventing it.")
        self.notify(
            f"✅ Attached airline complaint {saved.get('reference')} to "
            f"{self._flight_label(flight)}. {source_text} The GACA timer uses "
            f"the filing date and is due {due_text} if there is no substantive "
            "airline response.")

    def _status_text(self) -> str:
        counts = db.counts()
        mailbox = db.mailbox_cursor_summary()
        try:
            auto_days = max(1, int(self.settings.get(
                "gaca_auto_escalate_days", 7)))
        except (TypeError, ValueError):
            auto_days = 7
        if self.ai.enabled and self.ai.last_error:
            ai_status = (f" {self.ai.name} is configured on {self.ai.model}, "
                         f"but its last request failed: {self.ai.last_error}.")
        elif self.ai.enabled:
            ai_status = f" {self.ai.name} AI is configured on {self.ai.model}."
            if self.ai.settings.get("extract_profile_evidence", True):
                ai_status += (
                    " Guarded Telegram ticket/PDF import and passenger-profile "
                    "review are enabled.")
        else:
            ai_status = " AI assistance is off."
        captcha_status = (" 2Captcha is configured with Telegram fallback."
                          if self.captcha.enabled
                          else " Automatic CAPTCHA solving is off.")
        if self._mail_scan_thread and self._mail_scan_thread.is_alive():
            mailbox_status = " Gmail incremental sync is running now."
        elif self._mail_scan_error:
            mailbox_status = (
                f" Gmail's last incremental sync failed: "
                f"{self._mail_scan_error}.")
        elif mailbox.get("last_success"):
            mailbox_status = (
                f" Gmail last synced successfully at "
                f"{mailbox['last_success']} across "
                f"{mailbox.get('folders') or 0} folder(s).")
        else:
            mailbox_status = " Gmail incremental sync is waiting to start."
        status_settings = self.config.get("flight_status") or {}
        status_sources = []
        if status_settings.get("flightaware_api_key"):
            status_sources.append("FlightAware")
        if status_settings.get("airplanes_live_enabled", True):
            status_sources.append("Airplanes.live")
        if status_settings.get("adsb_lol_enabled", True):
            status_sources.append("adsb.lol")
        if status_settings.get("weather_enabled", True):
            status_sources.append("aviation weather")
        status_counts = db.flight_status_counts()
        tracking_status = (
            f" Live flight evidence is on through {', '.join(status_sources)}; "
            f"{status_counts['snapshots']} flight status snapshot(s) and "
            f"{status_counts['observations']} source observation(s) are persisted."
            if status_sources else
            " Live flight evidence is using booking and schedule data only.")
        gaca_sync = db.get_gaca_account_sync()
        if gaca_sync.get("last_success_at"):
            gaca_status = (
                f" GACA account last synced at "
                f"{gaca_sync['last_success_at']}; "
                f"{gaca_sync.get('cases_seen') or 0} regulator case(s) "
                "were seen.")
        elif gaca_sync.get("status") == "auth_required":
            gaca_status = (
                " GACA account sync needs a fresh Nafath login; send /gaca.")
        else:
            gaca_status = " GACA account sync is waiting for its first run."
        return (
            f"FlightDeck is running. {counts['flights']} flights, "
            f"{counts['complaints']} complaints, {counts['emails']} parsed emails."
            + mailbox_status + tracking_status + ai_status + captcha_status
            + gaca_status
            + f" GACA auto-escalation is on after {auto_days} days "
              "without a substantive airline response.")

    def _send_status(self):
        self.notify(self._status_text())

    def _gaca_cases_buttons(self) -> dict | None:
        settings = self.config.get("web") or {}
        base_url = str(settings.get("public_base_url") or "").rstrip("/")
        secret = str(settings.get("access_secret") or "")
        if not base_url or not secret:
            return None
        token = create_web_token(secret, self.chat_id)
        return {"inline_keyboard": [[{
            "text": "Open mapped GACA cases",
            "url": f"{base_url}/gaca-cases?access={token}",
        }]]}

    def start_gaca_account_sync(self, *, manual: bool = True) -> bool:
        """Start one account import; a manual run may request Nafath approval."""
        with self._gaca_sync_lock:
            if self._gaca_sync_thread and self._gaca_sync_thread.is_alive():
                if manual:
                    self.notify(
                        "The GACA account sync is already running. I will "
                        "send the result when it finishes.")
                return False
            if manual:
                self.notify(
                    "Starting a read-only GACA account sync. I will ask for "
                    "Nafath approval only if the saved session has expired.")
            self._gaca_sync_thread = threading.Thread(
                target=self._run_gaca_account_sync,
                args=(manual,),
                name="gaca-account-sync",
                daemon=True,
            )
            self._gaca_sync_thread.start()
        return True

    def _run_gaca_account_sync(self, manual: bool):
        last_stage = {"value": ""}

        def progress(stage: str, message: str, image: bytes | None = None):
            # Automatic refreshes are quiet unless they discover something.
            if not manual:
                return
            if stage == last_stage["value"] and not image:
                return
            last_stage["value"] = stage
            if image:
                self._send_photo(image, message)
            elif stage not in {"submitted", "error"}:
                self.notify(message)

        result = sync_gaca_account(
            self.config, progress, allow_login=manual)
        if result.status == "success":
            should_report = manual or result.new_cases or result.cases_reconciled
            if should_report:
                image = b""
                if result.screenshot_file:
                    try:
                        image = Path(result.screenshot_file).read_bytes()
                    except OSError:
                        image = b""
                if image:
                    self._send_photo(
                        image, result.message,
                        buttons=self._gaca_cases_buttons())
                else:
                    self.notify(
                        result.message, buttons=self._gaca_cases_buttons())
        elif manual:
            self.notify(result.message)

    def _maybe_sync_gaca_account(self):
        settings = self.config.get("gaca_account") or {}
        if not settings.get("enabled", True):
            return
        try:
            interval = max(5, int(settings.get("sync_minutes", 30))) * 60
        except (TypeError, ValueError):
            interval = 30 * 60
        now = time.monotonic()
        if now - self._last_gaca_sync < interval:
            return
        self._last_gaca_sync = now
        self.start_gaca_account_sync(manual=False)

    @staticmethod
    def _search_key(value: object) -> str:
        return "".join(character.casefold() for character in str(value or "")
                       if character.isalnum())

    @staticmethod
    def _effective_flight_value(flight: dict, field: str):
        overrides = flight.get("overrides") or {}
        return overrides.get(field) if overrides.get(field) not in {
            None, ""} else flight.get(field)

    def _flight_passenger(self, flight: dict) -> str:
        value = self._effective_flight_value(flight, "passenger") or ""
        return re.sub(r"\s+e[\s-]*ticket\b.*$", "", str(value),
                      flags=re.IGNORECASE).strip()

    def _profiles(self) -> list[dict]:
        profiles, seen = [], set()
        owner = dict(self.config.get("user") or {})
        owner_name = (owner.get("full_name") or " ".join(filter(None, (
            owner.get("first_name"), owner.get("middle_name"),
            owner.get("last_name")))))
        if owner_name:
            owner["booking_name"] = owner_name
            key = passenger_profile_key(owner_name)
            profiles.append(owner)
            seen.add(key)
        for raw_key, value in (self.config.get("passengers") or {}).items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            name = (item.get("booking_name") or item.get("full_name")
                    or str(raw_key))
            key = passenger_profile_key(name)
            if not key:
                continue
            item["booking_name"] = name
            if key in seen:
                owner_index = next((index for index, profile in enumerate(profiles)
                                    if passenger_profile_key(
                                        profile.get("booking_name")) == key), None)
                if owner_index is not None:
                    profiles[owner_index] = {
                        **profiles[owner_index],
                        **{field: content for field, content in item.items()
                           if content not in {None, ""}},
                    }
                continue
            profiles.append(item)
            seen.add(key)
        return profiles

    def _profile_for_flight(self, flight: dict) -> dict:
        passenger_key = passenger_profile_key(self._flight_passenger(flight))
        profiles = self._profiles()
        exact = next((profile for profile in profiles
                      if passenger_profile_key(profile.get("booking_name"))
                      == passenger_key), None)
        return exact or (profiles[0] if profiles and not passenger_key else {})

    def _remember_ai_context(self, *, flight: dict | None = None,
                             complaint: dict | None = None,
                             passenger: str = ""):
        if complaint:
            flight = complaint.get("flight_data") or flight
            self._ai_context["reference"] = complaint.get("reference") or ""
        elif flight:
            self._ai_context["reference"] = ""
        if flight:
            self._ai_context.update({
                "flight_number": self._effective_flight_value(
                    flight, "flight_number") or "",
                "pnr": self._effective_flight_value(flight, "pnr") or "",
                "passenger": self._flight_passenger(flight),
            })
        if passenger:
            self._ai_context["passenger"] = passenger
        self._ai_context["updated"] = time.monotonic()

    def _contextualize_ai_action(self, action: dict, message: str) -> dict:
        """Resolve short follow-ups from the last exact result, never by AI guess."""
        result = dict(action)
        detail_requested = re.search(
            r"\b(?:detail|details|everything|information|info|status)\b",
            message, re.IGNORECASE)
        if detail_requested and result.get("name") == "list_complaints":
            result["name"] = "complaint_details"
        elif detail_requested and result.get("name") == "list_flights":
            result["name"] = "flight_details"
        updated = float(self._ai_context.get("updated") or 0)
        if not updated or time.monotonic() - updated > 30 * 60:
            return result
        has_selector = any(result.get(field) for field in (
            "flight_number", "pnr", "reference", "passenger"))
        short_follow_up = len(message.split()) <= 7
        pronoun = re.search(
            r"\b(?:it|its|that|this|same|those|them|there)\b",
            message, re.IGNORECASE)
        if has_selector or not (short_follow_up or pronoun):
            return result
        name = result.get("name")
        complaint_actions = {
            "complaint_details", "complaint_responses", "show_evidence",
            "search_email",
        }
        flight_actions = {
            "flight_details", "list_flights", "flight_status",
            "case_recommendation", "complaint_readiness",
        }
        if name in complaint_actions and self._ai_context.get("reference"):
            result["reference"] = self._ai_context["reference"]
        elif name in complaint_actions:
            result["flight_number"] = self._ai_context.get(
                "flight_number") or ""
            result["pnr"] = self._ai_context.get("pnr") or ""
        elif name in flight_actions:
            result["flight_number"] = self._ai_context.get(
                "flight_number") or ""
            result["pnr"] = self._ai_context.get("pnr") or ""
        elif name == "profile_details":
            result["passenger"] = self._ai_context.get("passenger") or ""
        return result

    def _ai_catalog(self) -> dict:
        all_complaints = db.list_complaints()
        complaints_by_flight: dict[str, list[dict]] = {}
        for item in reversed(all_complaints):
            complaints_by_flight.setdefault(item.get("flight_key") or "", []).append(item)
        flights = []
        for flight in db.list_flights()[:20]:
            snapshot = db.get_flight_status_snapshot(flight.get("flight_key") or "") or {}
            strategy = recommend_case(
                flight, snapshot, complaints_by_flight.get(
                    flight.get("flight_key") or "", []))
            flights.append({
                "flight_number": self._effective_flight_value(
                    flight, "flight_number") or ", ".join(
                        flight.get("flight_numbers") or []),
                "date": self._effective_flight_value(flight, "flight_date") or "",
                "origin": self._effective_flight_value(flight, "origin") or "",
                "destination": self._effective_flight_value(
                    flight, "destination") or "",
                "pnr": self._effective_flight_value(flight, "pnr") or "",
                "passenger": self._flight_passenger(flight),
                "live_status": snapshot.get("status") or "not checked",
                "status_confidence": snapshot.get("confidence"),
                "status_provider": snapshot.get("provider") or "",
                "status_updated_at": snapshot.get("updated_at") or "",
                "recommended_action": strategy.get("recommended_action"),
                "complaint_readiness": strategy.get("readiness_score"),
            })
        complaints = []
        for complaint in all_complaints[:20]:
            flight = complaint.get("flight_data") or {}
            complaints.append({
                "reference": complaint.get("reference") or "",
                "kind": complaint.get("kind") or "",
                "status": complaint.get("status") or "",
                "created_at": complaint.get("created_at") or "",
                "portal_category": complaint.get("portal_category") or "",
                "issue": _clean_excerpt(complaint.get("details") or "", 240),
                "flight_number": self._effective_flight_value(
                    flight, "flight_number") or ", ".join(
                        flight.get("flight_numbers") or []),
                "passenger": self._flight_passenger(flight),
                "has_evidence": bool(complaint.get("attachments")),
            })
        screenshots = TELEGRAM_EVIDENCE_DIR / "portal_jobs"
        portal_jobs = []
        for job in db.list_portal_jobs(5):
            portal_jobs.append({
                key: job.get(key) for key in (
                    "id", "kind", "airline_code", "flight_number", "status",
                    "message", "reference", "terminal", "created_at",
                    "updated_at",
                )
            } | {"has_screenshot": bool(job.get("screenshot_file"))})
        gaca_account_cases = [{
            key: item.get(key) for key in (
                "reference", "status", "airline", "airline_reference",
                "flight_number", "flight_date", "ticket_number", "pnr",
                "passenger_name", "category", "mapping_status",
                "mapped_flight_key", "match_method",
            )
        } for item in db.list_gaca_account_cases(30)]
        with self._lock:
            verification_kind = (self._verification.kind
                                 if self._verification else "")
            intake_keys = list(self._intakes)[:5]
        pending_survey = db.pending_survey(self.chat_id)
        return {
            "counts": db.counts(),
            "mailbox": db.mailbox_cursor_summary(),
            "flights": flights,
            "complaints": complaints,
            "gaca_account_cases": gaca_account_cases,
            "gaca_account_sync": db.get_gaca_account_sync(),
            "passengers": [{
                "name": profile.get("booking_name") or "",
                "role": "owner" if index == 0 else "family",
            } for index, profile in enumerate(self._profiles())],
            "available_images": {
                "complaint_evidence": sum(
                    len(item.get("attachments") or [])
                    for item in all_complaints),
                "portal_screenshots": len(list(screenshots.glob("*.png")))
                if screenshots.exists() else 0,
            },
            "recent_portal_jobs": portal_jobs,
            "recent_conversation": db.list_telegram_messages(16),
            "workflow_state": {
                "pending_survey": ({
                    "flight_key": pending_survey.get("flight_key"),
                    "status": pending_survey.get("status"),
                    "updated_at": pending_survey.get("updated_at"),
                } if pending_survey else None),
                "active_intake_flight_keys": intake_keys,
                "verification_kind": verification_kind,
                "mail_scan_running": bool(
                    self._mail_scan_thread
                    and self._mail_scan_thread.is_alive()),
            },
            "context": {
                key: value for key, value in self._ai_context.items()
                if key != "updated"
            },
        }

    def _dispatch_ai_message(self, text: str):
        self.notify(f"{self.ai.name} is checking your FlightDeck recordsâ€¦")
        thread = threading.Thread(
            target=self._run_ai_message, args=(text,),
            name="telegram-ai-request", daemon=True)
        self._ai_chat_thread = thread
        thread.start()

    def _run_ai_message(self, text: str):
        with self._ai_chat_lock:
            try:
                intent = self.ai.interpret_telegram(text, self._ai_catalog())
                if not isinstance(intent, dict):
                    error = getattr(self.ai, "last_error", "")
                    suffix = f" ({error})" if error else ""
                    self.notify(
                        f"{self.ai.name} could not interpret that request right "
                        f"now{suffix}. /status and /web still work normally.")
                    return
                sent = False
                for action in (intent.get("actions") or [])[:3]:
                    if isinstance(action, dict):
                        action = self._contextualize_ai_action(action, text)
                        sent = self._execute_ai_action(action) or sent
                reply = _clean_excerpt(str(intent.get("reply") or ""), 3000)
                if reply:
                    self.notify(reply)
                    sent = True
                if not sent:
                    self.notify(
                        "I could not map that to a FlightDeck lookup. Try naming "
                        "a flight number, PNR, passenger, or complaint reference.")
            except Exception:
                logger.exception("Telegram AI request failed")
                self.notify(
                    "I could not finish that lookup. No complaint or stored record "
                    "was changed; /status and /web still work normally.")

    def _matching_flights(self, action: dict) -> list[dict]:
        flights = db.list_flights()
        number = self._search_key(action.get("flight_number"))
        pnr = self._search_key(action.get("pnr"))
        passenger = passenger_profile_key(action.get("passenger") or "")
        if number:
            flights = [flight for flight in flights if number in {
                self._search_key(self._effective_flight_value(
                    flight, "flight_number")),
                *(self._search_key(value)
                  for value in (flight.get("flight_numbers") or [])),
            }]
        if pnr:
            flights = [flight for flight in flights
                       if self._search_key(self._effective_flight_value(
                           flight, "pnr")) == pnr]
        if passenger:
            selected = []
            for flight in flights:
                flight_passenger = passenger_profile_key(
                    self._flight_passenger(flight))
                if (passenger in flight_passenger
                        or (flight_passenger and flight_passenger in passenger)):
                    selected.append(flight)
            flights = selected
        scope = action.get("time_scope") or "all"
        now = datetime.now()
        if scope in {"upcoming", "past"}:
            selected = []
            for flight in flights:
                when = (parse_flight_time(self._effective_flight_value(
                    flight, "departure")) or parse_flight_time(
                        self._effective_flight_value(flight, "flight_date")))
                if when and ((scope == "upcoming" and when >= now)
                             or (scope == "past" and when < now)):
                    selected.append(flight)
            flights = selected
            flights.sort(key=lambda flight: (
                parse_flight_time(self._effective_flight_value(
                    flight, "departure")) or parse_flight_time(
                        self._effective_flight_value(flight, "flight_date"))
                or datetime.max), reverse=scope == "past")
        query = " ".join(str(action.get("query") or "").casefold().split())
        generic = {"", "flight", "flights", "details", "flight details",
                   "info", "information", "next", "upcoming", "past",
                   "previous", "latest", "last"}
        if query not in generic:
            matches = []
            for flight in flights:
                haystack = " ".join(str(value or "") for value in (
                    self._effective_flight_value(flight, "flight_number"),
                    *(flight.get("flight_numbers") or []),
                    self._effective_flight_value(flight, "flight_date"),
                    self._effective_flight_value(flight, "origin"),
                    self._effective_flight_value(flight, "destination"),
                    self._effective_flight_value(flight, "pnr"),
                    self._flight_passenger(flight),
                    flight.get("airline_name"),
                )).casefold()
                if query in haystack:
                    matches.append(flight)
            flights = matches
        return flights

    def _matching_complaints(self, action: dict) -> list[dict]:
        complaints = db.list_complaints()
        reference = self._search_key(action.get("reference"))
        if reference:
            complaints = [item for item in complaints
                          if self._search_key(item.get("reference")) == reference]
        number = self._search_key(action.get("flight_number")).upper()
        pnr = self._search_key(action.get("pnr"))
        passenger = passenger_profile_key(action.get("passenger") or "")
        if number:
            complaints = [item for item in complaints
                          if number in self._complaint_flight_numbers(item)]
        if pnr:
            complaints = [item for item in complaints
                          if self._search_key(self._effective_flight_value(
                              item.get("flight_data") or {}, "pnr")) == pnr]
        if passenger:
            complaints = [item for item in complaints
                          if passenger in passenger_profile_key(
                              self._flight_passenger(
                                  item.get("flight_data") or {}))]
        query = " ".join(str(action.get("query") or "").casefold().split())
        generic = {"", "complaint", "complaints", "case", "cases", "details",
                   "latest", "last", "response", "responses", "evidence",
                   "photo", "photos", "pictures"}
        if query not in generic:
            complaints = [item for item in complaints if query in " ".join(
                str(value or "") for value in (
                    item.get("reference"), item.get("kind"), item.get("status"),
                    item.get("subject"), item.get("details"),
                    item.get("portal_category"), item.get("submitted_text"),
                    self._flight_label(item.get("flight_data") or {}),
                    self._flight_passenger(item.get("flight_data") or {}),
                )).casefold()]
        return complaints

    def _flight_summary(self, flight: dict) -> str:
        number = (self._effective_flight_value(flight, "flight_number")
                  or ", ".join(flight.get("flight_numbers") or [])
                  or "Flight unknown")
        date = self._effective_flight_value(flight, "flight_date") or "date unknown"
        origin = self._effective_flight_value(flight, "origin") or "?"
        destination = self._effective_flight_value(flight, "destination") or "?"
        passenger = self._flight_passenger(flight) or "passenger unknown"
        pnr = self._effective_flight_value(flight, "pnr") or "PNR unknown"
        return f"{number} | {date} | {origin} â†’ {destination} | {passenger} | {pnr}"

    def _complaint_summary(self, complaint: dict) -> str:
        reference = complaint.get("reference") or "reference pending"
        return (f"{reference} | {complaint.get('kind') or 'case'} | "
                f"{complaint.get('status') or 'unknown'} | "
                f"{self._flight_label(complaint.get('flight_data') or {})} | "
                f"{(complaint.get('created_at') or '')[:16]}")

    def _send_flight_details(self, action: dict) -> bool:
        flights = self._matching_flights(action)
        if action.get("latest") and flights:
            flights = flights[:1]
        if not flights:
            self.notify("I found no stored flight matching those exact details.")
            return True
        if len(flights) > 1:
            lines = ["I found several flights. Name the flight number or PNR:"]
            lines.extend(f"â€¢ {self._flight_summary(item)}" for item in flights[:8])
            self.notify("\n".join(lines))
            return True
        flight = db.get_flight(flights[0]["id"]) or flights[0]
        self._remember_ai_context(flight=flight)
        payment = self._effective_flight_value(flight, "payment_method") or "not found"
        text = "\n".join((
            f"Flight: {self._flight_summary(flight)}",
            f"Airline: {flight.get('airline_name') or flight.get('airline_code') or 'unknown'}",
            f"Departure: {self._effective_flight_value(flight, 'departure') or 'unknown'}",
            f"Arrival: {self._effective_flight_value(flight, 'arrival') or 'unknown'}",
            f"Ticket: {self._effective_flight_value(flight, 'ticket_number') or 'not found'}",
            f"Payment: {payment}",
            f"Linked emails: {len(flight.get('emails') or [])}",
            f"Complaints: {len(flight.get('complaints') or [])}",
        ))
        self.notify(text)
        return True

    def _selected_flight(self, action: dict) -> dict | None:
        flights = self._matching_flights(action)
        if action.get("latest") and flights:
            flights = flights[:1]
        if not flights:
            self.notify("I found no stored flight matching those exact details.")
            return None
        if len(flights) > 1:
            self.notify("I found several flights. Name the flight number or PNR:\n" +
                        "\n".join(f"• {self._flight_summary(item)}"
                                  for item in flights[:8]))
            return None
        return db.get_flight(flights[0]["id"]) or flights[0]

    def _send_live_status(self, action: dict, force: bool = True) -> bool:
        flight = self._selected_flight(action)
        if not flight:
            return True
        self.notify(f"Checking live sources for {self._flight_label(flight)}…")
        snapshot = refresh_flight_status(
            self.config, flight, force=force, now=self._flight_status_now())
        self._remember_ai_context(flight=flight)
        sources = snapshot.get("sources") or []
        source_text = ", ".join(
            f"{item.get('provider')} ({item.get('status')})" for item in sources[:5])
        lines = [
            f"Flight status: {self._flight_summary(flight)}",
            f"Current state: {snapshot.get('label') or 'Unknown'}",
            f"Confidence/source: {int(float(snapshot.get('confidence') or 0) * 100)}% / "
            f"{snapshot.get('provider') or 'none'}",
            f"Last checked: {snapshot.get('updated_at') or 'unknown'}",
        ]
        for label, key in (("Estimated departure", "estimated_departure"),
                           ("Actual departure", "actual_departure"),
                           ("Estimated arrival", "estimated_arrival"),
                           ("Actual arrival", "actual_arrival")):
            if snapshot.get(key):
                lines.append(f"{label}: {snapshot[key]}")
        position = snapshot.get("position") or {}
        if position:
            lines.append("Position: " + ", ".join(
                f"{key}={value}" for key, value in position.items()))
        if source_text:
            lines.append(f"Evidence: {source_text}")
        if snapshot.get("contradictions"):
            lines.append("Warning: " + " ".join(snapshot["contradictions"]))
        if snapshot.get("errors"):
            lines.append("Unavailable sources: " + "; ".join(snapshot["errors"]))
        if snapshot.get("provider") == "schedule":
            lines.append("Schedule-only means the bot has not independently verified movement or arrival.")
        self.notify("\n".join(lines))
        return True

    def _strategy_for_flight(self, flight: dict, *, refresh: bool = False) -> dict:
        snapshot = get_flight_status(
            self.config, flight, refresh=refresh, now=self._flight_status_now())
        complaints = db.complaints_for_flight(flight.get("flight_key") or "")
        complaint_ids = {item["id"] for item in complaints}
        responses = [item for item in db.complaint_response_details(50)
                     if item.get("complaint_id") in complaint_ids]
        return recommend_case(
            flight, snapshot, complaints, responses,
            now=self._flight_local_now(),
            gaca_days=max(1, int(self.settings.get("gaca_auto_escalate_days", 7))))

    def _send_case_recommendation(self, action: dict, *, readiness: bool = False,
                                  question: str = "") -> bool:
        flight = self._selected_flight(action)
        if not flight:
            return True
        strategy = self._strategy_for_flight(flight, refresh=True)
        self._remember_ai_context(flight=flight)
        if readiness:
            lines = [
                f"Complaint readiness for {self._flight_label(flight)}: "
                f"{strategy['readiness_score']}%",
            ]
            if strategy.get("missing_facts"):
                lines.append("Missing facts:\n• " + "\n• ".join(strategy["missing_facts"]))
            lines.append("Evidence checklist:\n• " +
                         "\n• ".join(strategy["evidence_checklist"]))
            self.notify("\n".join(lines))
            return True

        explanation = None
        if self.ai.enabled:
            explanation = self.ai.explain_case_recommendation(
                question or str(action.get("query") or "What should I do?"),
                flight, strategy)
        lines = [
            f"Best course for {self._flight_label(flight)}: "
            f"{strategy['label']}",
            f"Status evidence: {(strategy.get('status') or {}).get('label') or 'Unknown'} "
            f"via {(strategy.get('status') or {}).get('provider') or 'none'}",
        ]
        if explanation:
            lines.append(explanation.get("summary") or "")
            lines.extend(f"• {reason}" for reason in explanation.get("why") or [])
            if explanation.get("next_question"):
                lines.append("Question: " + explanation["next_question"])
        else:
            lines.extend(f"• {reason}" for reason in strategy.get("reasons") or [])
            if strategy.get("missing_facts"):
                lines.append("Most important missing fact: " + strategy["missing_facts"][0])
        lines.append("Recommended remedy: " + strategy["requested_remedy"])
        if strategy.get("next_review_at"):
            lines.append("Next review: " + strategy["next_review_at"])
        if strategy.get("filing_deadline"):
            lines.append("GACA incident filing deadline: " + strategy["filing_deadline"])
        self.notify("\n".join(line for line in lines if line))
        return True

    def _send_complaint_details(self, action: dict) -> bool:
        complaints = self._matching_complaints(action)
        if action.get("latest") and complaints:
            complaints = complaints[:1]
        if not complaints:
            self.notify("I found no complaint matching those exact details.")
            return True
        if len(complaints) > 1:
            lines = ["I found several complaints. Name the reference or flight:"]
            lines.extend(f"â€¢ {self._complaint_summary(item)}"
                         for item in complaints[:8])
            self.notify("\n".join(lines))
            return True
        complaint = complaints[0]
        flight = complaint.get("flight_data") or {}
        self._remember_ai_context(complaint=complaint)
        profile = self._profile_for_flight(flight)
        responses = [item for item in db.complaint_response_details(50)
                     if item["complaint_id"] == complaint["id"]]
        due_text = "not applicable"
        if complaint.get("kind") == "airline":
            created = parse_flight_time(complaint.get("created_at"))
            try:
                days = max(1, int(self.settings.get(
                    "gaca_auto_escalate_days", 7)))
            except (TypeError, ValueError):
                days = 7
            due_text = ((created + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
                        if created else "unknown")
        self.notify("\n".join((
            f"Complaint: {complaint.get('reference') or 'reference pending'}",
            f"Type/status: {complaint.get('kind') or 'unknown'} / {complaint.get('status') or 'unknown'}",
            f"Flight: {self._flight_summary(flight)}",
            f"Created: {complaint.get('created_at') or 'unknown'}",
            f"Portal category: {complaint.get('portal_category') or 'not recorded'}",
            f"Issue: {_clean_excerpt(complaint.get('details') or 'not recorded', 900)}",
            "Text sent to portal: " + _clean_excerpt(
                complaint.get("submitted_text") or
                "not recorded by the older filing version", 1800),
            f"Evidence files: {len(complaint.get('attachments') or [])}",
            f"Matched airline responses: {len(responses)}",
            f"GACA auto-escalation due: {due_text}",
            "Current configured passenger email: "
            f"{profile.get('email') or 'not found'}",
        )))
        return True

    def _send_response_details(self, action: dict) -> bool:
        selected = self._matching_complaints(action)
        has_selector = any(action.get(field) for field in (
            "reference", "flight_number", "pnr", "passenger", "query"))
        selected_ids = {item["id"] for item in selected}
        rows = db.complaint_response_details(50)
        if has_selector:
            rows = [row for row in rows if row["complaint_id"] in selected_ids]
        if action.get("latest") and rows:
            rows = rows[:1]
        limit = max(1, min(int(action.get("limit") or 5), 10))
        rows = rows[:limit]
        if not rows:
            self.notify("No matched airline response was found for that complaint.")
            return True
        complaint_map = {item["id"]: item for item in db.list_complaints()}
        first_complaint = complaint_map.get(rows[0]["complaint_id"])
        if first_complaint:
            self._remember_ai_context(complaint=first_complaint)
        for row in rows:
            self.notify("\n".join((
                f"Airline response for {row.get('reference') or 'case'}",
                f"Date: {row.get('date') or 'unknown'}",
                f"From: {row.get('sender') or 'unknown'}",
                f"Subject: {row.get('subject') or '(no subject)'}",
                f"Matched by: {str(row.get('match_method') or '').replace('_', ' ')}",
                f"Message: {_clean_excerpt(row.get('body') or '', 1200)}",
            )))
        return True

    def _send_email_search(self, action: dict) -> bool:
        query = (action.get("query") or action.get("reference")
                 or action.get("flight_number") or action.get("pnr") or "")
        limit = max(1, min(int(action.get("limit") or 5), 10))
        rows = db.search_mail_events(query, limit)
        if not rows:
            self.notify(f"No stored airline email matched {query!r}.")
            return True
        lines = [f"Stored airline emails matching {query!r}:"
                 if query else "Latest stored airline emails:"]
        for row in rows:
            lines.append(
                f"â€¢ {(row.get('date') or '')[:16]} | "
                f"{row.get('sender') or 'unknown sender'} | "
                f"{row.get('subject') or '(no subject)'}\n  "
                f"{_clean_excerpt(row.get('body') or '', 300)}")
        self.notify("\n".join(lines))
        return True

    def _send_evidence(self, action: dict) -> bool:
        complaints = self._matching_complaints(action)
        if action.get("latest") and complaints:
            complaints = complaints[:1]
        paths = []
        for complaint in complaints:
            for value in complaint.get("attachments") or []:
                path = Path(value)
                if (path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
                        and path.is_file()):
                    paths.append((path, complaint))
        limit = max(1, min(int(action.get("limit") or 5), 10))
        if not paths:
            self.notify("No saved complaint photos matched that request.")
            return True
        self._remember_ai_context(complaint=paths[0][1])
        for path, complaint in paths[:limit]:
            try:
                self._send_photo(
                    path.read_bytes(),
                    f"Evidence for {complaint.get('reference') or self._flight_label(complaint.get('flight_data') or {})}")
            except OSError:
                logger.exception("Could not read Telegram evidence %s", path)
        return True

    def _send_latest_screenshots(self, action: dict) -> bool:
        folder = TELEGRAM_EVIDENCE_DIR / "portal_jobs"
        paths = sorted(folder.glob("*.png"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        limit = max(1, min(int(action.get("limit") or 1), 5))
        if not paths:
            self.notify("No saved portal screenshot is available yet.")
            return True
        sent = 0
        for path in paths[:limit]:
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S")
                self._send_photo(
                    path.read_bytes(),
                    f"Saved portal screenshot from {stamp}")
                sent += 1
            except OSError:
                logger.exception("Could not read portal screenshot %s", path)
        if not sent:
            self.notify("The saved portal screenshot could not be read.")
        return True

    def _send_profile(self, action: dict) -> bool:
        profiles = self._profiles()
        selector = passenger_profile_key(action.get("passenger") or "")
        if selector:
            profiles = [profile for profile in profiles
                        if selector in passenger_profile_key(
                            profile.get("booking_name") or "")]
        elif profiles:
            # profile_details without a named passenger means the configured
            # owner. list_passengers remains the explicit family-wide action.
            profiles = profiles[:1]
        if action.get("latest") and profiles:
            profiles = profiles[:1]
        if not profiles:
            self.notify("No stored passenger profile matched that name.")
            return True
        if len(profiles) > 1:
            self.notify("Stored passengers:\n" + "\n".join(
                f"â€¢ {profile.get('booking_name') or 'Unnamed passenger'}"
                for profile in profiles))
            return True
        profile = profiles[0]
        self._remember_ai_context(
            passenger=profile.get("booking_name") or "")
        full_name = profile.get("full_name") or " ".join(filter(None, (
            profile.get("first_name"), profile.get("middle_name"),
            profile.get("last_name")))) or profile.get("booking_name")
        self.notify("\n".join((
            f"Passenger: {full_name or 'unknown'}",
            f"Booking name: {profile.get('booking_name') or 'unknown'}",
            f"Title: {profile.get('title') or 'not found'}",
            f"Email: {profile.get('email') or 'not found'}",
            f"Phone: {(profile.get('country_code') or '')} {profile.get('phone') or 'not found'}".strip(),
            f"Nationality: {profile.get('nationality') or 'not found'}",
            f"National ID/passport/Iqama: {profile.get('national_id') or 'not found'}",
            f"Alfursan ID: {profile.get('alfursan_id') or 'not found'}",
        )))
        return True

    def _send_gaca_account_cases(self, action: dict) -> bool:
        rows = db.list_gaca_account_cases()
        reference = self._search_key(action.get("reference"))
        flight_number = self._search_key(action.get("flight_number"))
        pnr = self._search_key(action.get("pnr"))
        passenger = passenger_profile_key(action.get("passenger") or "")
        query = str(action.get("query") or "").strip().casefold()
        if reference:
            rows = [item for item in rows if reference in {
                self._search_key(item.get("reference")),
                self._search_key(item.get("airline_reference")),
            }]
        if flight_number:
            rows = [item for item in rows if self._search_key(
                item.get("flight_number")) == flight_number]
        if pnr:
            rows = [item for item in rows if self._search_key(
                item.get("pnr")) == pnr]
        if passenger:
            rows = [item for item in rows if passenger in passenger_profile_key(
                item.get("passenger_name") or "")]
        if query:
            rows = [item for item in rows if query in " ".join(
                str(item.get(field) or "") for field in (
                    "reference", "status", "airline", "airline_reference",
                    "flight_number", "flight_date", "ticket_number", "pnr",
                    "passenger_name", "category", "mapping_status",
                )).casefold()]
        if action.get("latest") and rows:
            rows = rows[:1]
        limit = max(1, min(int(action.get("limit") or 8), 10))
        if not rows:
            state = db.get_gaca_account_sync()
            if state.get("status") in {"never_synced", "auth_required"}:
                self.notify(
                    "No imported GACA account cases are available yet. Send "
                    "/gaca to sign in with Nafath and synchronize them.")
            else:
                self.notify(
                    "No imported GACA account case matched those exact details.")
            return True
        lines = ["GACA account cases:"]
        for item in rows[:limit]:
            lines.append(
                f"• {item.get('reference') or 'reference unavailable'} | "
                f"{item.get('status') or 'status unavailable'} | "
                f"{item.get('airline') or 'airline unavailable'} "
                f"{item.get('flight_number') or ''} | "
                f"airline ref {item.get('airline_reference') or 'not shown'} | "
                f"{item.get('passenger_name') or 'passenger not shown'} | "
                f"{str(item.get('mapping_status') or 'unmapped').replace('_', ' ')}"
            )
        self.notify(
            "\n".join(lines), buttons=self._gaca_cases_buttons())
        return True

    def _execute_ai_action(self, action: dict) -> bool:
        name = action.get("name")
        if name == "status":
            self._send_status()
            return True
        if name == "web_link":
            self._send_web_link()
            return True
        if name == "scan_mailbox":
            result = self._maybe_scan_mailbox(force=True, notify_when_done=True)
            messages = {
                "started": "Gmail incremental sync started. I will tell you when it finishes.",
                "running": "Gmail incremental sync is already running.",
                "not_configured": "Gmail sync is not configured on this server.",
            }
            self.notify(messages.get(result, "Gmail sync did not need to start."))
            return True
        if name in {"list_flights", "flight_details"}:
            if name == "flight_details":
                return self._send_flight_details(action)
            flights = self._matching_flights(action)
            if action.get("latest") and flights:
                flights = flights[:1]
            limit = max(1, min(int(action.get("limit") or 8), 10))
            if not flights:
                self.notify("No stored flight matched that request.")
            else:
                if len(flights[:limit]) == 1:
                    self._remember_ai_context(flight=flights[0])
                self.notify("Flights:\n" + "\n".join(
                    f"â€¢ {self._flight_summary(item)}"
                    for item in flights[:limit]))
            return True
        if name == "flight_status":
            return self._send_live_status(action, force=True)
        if name == "case_recommendation":
            return self._send_case_recommendation(
                action, question=str(action.get("query") or ""))
        if name == "complaint_readiness":
            return self._send_case_recommendation(action, readiness=True)
        if name in {"list_complaints", "complaint_details"}:
            if name == "complaint_details":
                return self._send_complaint_details(action)
            complaints = self._matching_complaints(action)
            if action.get("latest") and complaints:
                complaints = complaints[:1]
            limit = max(1, min(int(action.get("limit") or 8), 10))
            if not complaints:
                self.notify("No stored complaint matched that request.")
            else:
                if len(complaints[:limit]) == 1:
                    self._remember_ai_context(complaint=complaints[0])
                self.notify("Complaints:\n" + "\n".join(
                    f"â€¢ {self._complaint_summary(item)}"
                    for item in complaints[:limit]))
            return True
        if name == "gaca_account_cases":
            return self._send_gaca_account_cases(action)
        if name == "sync_gaca_account":
            self.start_gaca_account_sync(manual=True)
            return True
        if name == "complaint_responses":
            return self._send_response_details(action)
        if name == "search_email":
            return self._send_email_search(action)
        if name == "show_evidence":
            return self._send_evidence(action)
        if name == "latest_screenshot":
            return self._send_latest_screenshots(action)
        if name in {"portal_status", "explain_portal_failure"}:
            return self._send_portal_job(
                action, explain=name == "explain_portal_failure")
        if name == "profile_details":
            return self._send_profile(action)
        if name == "list_passengers":
            profiles = self._profiles()
            names = [profile.get("booking_name") or "Unnamed passenger"
                     for profile in profiles]
            self.notify("Stored passengers:\n" + ("\n".join(
                f"â€¢ {name}" for name in names) if names else "None found."))
            return True
        if name == "help":
            self.notify(
                "Ask naturally about flights, PNRs, passengers, complaint "
                "references, airline responses, stored email, evidence photos, "
                "portal screenshots, live flight status, complaint readiness, the "
                "best next action, a fresh Gmail sync, or GACA account cases. "
                "Existing /status, /gaca, /web, /cancel, post-flight, "
                "verification, and complaint "
                "flows keep priority.")
            return True
        return False

    def _send_portal_job(self, action: dict, explain: bool = False) -> bool:
        jobs = db.list_portal_jobs(1)
        if not jobs:
            self.notify(
                "No persisted portal-job timeline is available yet. I can still "
                "send the latest saved screenshot if one exists.")
            return self._send_latest_screenshots({"limit": 1})
        job = jobs[0]
        reference = str(job.get("reference") or "").strip()
        lines = [
            f"Latest portal job: {job.get('flight_number') or job.get('airline_code') or job.get('kind') or 'complaint'}",
            f"Stage: {job.get('status') or 'unknown'}",
            f"Result: {job.get('message') or 'No result message was saved.'}",
            f"Reference: {reference or 'none returned'}",
            f"Updated: {job.get('updated_at') or 'unknown'}",
        ]
        path = Path(str(job.get("screenshot_file") or ""))
        image = None
        if path.is_file():
            try:
                image = path.read_bytes()
            except OSError:
                logger.exception("Could not read portal screenshot %s", path)
        if explain and self.ai.enabled:
            ai_job = {
                **job,
                "automatic_captcha_enabled": self.captcha.enabled,
                "telegram_fallback_enabled": bool(
                    self.captcha.settings.get("telegram_fallback", True)),
            }
            analysis = self.ai.analyze_portal_failure(
                str(action.get("query") or "Why did the portal job fail?"),
                ai_job, db.list_telegram_messages(12), image=image)
            if analysis:
                lines.extend((
                    f"Visible state: {analysis.get('visible_state') or 'not clear'}",
                    f"Likely cause: {analysis.get('likely_cause') or 'not established'}",
                    f"Next step: {analysis.get('next_step') or 'Review before retrying.'}",
                ))
        self.notify("\n".join(lines))
        if image:
            self._send_photo(image, "Screenshot saved for this portal job")
        return True

    @staticmethod
    def _complaint_flight_numbers(complaint: dict) -> set[str]:
        flight = complaint.get("flight_data") or {}
        values = list(flight.get("flight_numbers") or [])
        override = (flight.get("overrides") or {}).get("flight_number")
        if override:
            values.append(override)
        if flight.get("flight_number"):
            values.append(flight["flight_number"])
        return {re.sub(r"[^A-Z0-9]", "", str(value).upper())
                for value in values if value}

    def _capture_telegram_reference(self, reference: str, pasted: str,
                                    message: dict) -> bool:
        complaints = db.list_complaints()
        existing = next((item for item in complaints
                         if str(item.get("reference") or "").casefold()
                         == reference.casefold()), None)
        if existing:
            self.notify(
                f"Reference {reference} is already saved. No duplicate change was made.")
            return True
        pending = [item for item in complaints
                   if item.get("kind") == "airline"
                   and item.get("status") == "accepted_pending_reference"
                   and not item.get("reference")
                   and (item.get("flight_data") or {}).get("airline_code") == "SV"]
        if not pending:
            self.notify(
                f"I found reference {reference}, but there is no pending Saudia "
                "complaint to attach it to. Nothing was changed.")
            return True
        context = " ".join((
            pasted,
            str((message.get("reply_to_message") or {}).get("text") or ""),
        ))
        mentioned = {
            re.sub(r"\s+", "", value).upper()
            for value in re.findall(r"\bSV\s*\d{3,4}\b", context, re.I)
        }
        if mentioned:
            matched = [item for item in pending
                       if self._complaint_flight_numbers(item) & mentioned]
            if len(matched) == 1:
                pending = matched
        if len(pending) != 1:
            self.notify(
                f"I extracted {reference}, but more than one Saudia complaint "
                "is waiting for a reference. Reply with the SMS plus the flight "
                "number, for example SV1671. Nothing was changed.",
                force_reply=True)
            return True
        complaint = pending[0]
        created_at = complaint.get("created_at") or ""
        db.finish_complaint(complaint["id"], "submitted", reference)
        db.mark_event_seen(f"reference-captured:telegram:{reference.casefold()}")
        flight = complaint.get("flight_data") or {}
        label = next(iter(self._complaint_flight_numbers(complaint)), "Saudia flight")
        try:
            delay_days = max(1, int(self.settings.get(
                "gaca_auto_escalate_days", 7)))
        except (TypeError, ValueError):
            delay_days = 7
        created = parse_flight_time(created_at)
        due = created + timedelta(days=delay_days) if created else None
        due_text = due.strftime("%Y-%m-%d %H:%M") if due else "the saved due date"
        self.notify(
            f"Saved {reference} as the airline complaint reference for {label}. "
            "I stored only the reference, not the pasted SMS. The GACA "
            f"seven-day countdown still starts from the original submission "
            f"time ({created_at}); automatic escalation is due {due_text} if "
            "Saudia does not provide a substantive response.")
        return True

    def ask_for_pending_references(self, now: datetime | None = None):
        """After an email grace period, ask once for a missing Saudia ref."""
        now = now or datetime.now()
        try:
            grace_minutes = max(2, int(self.settings.get(
                "mailbox_scan_minutes", 10)))
        except (TypeError, ValueError):
            grace_minutes = 10
        cutoff = now - timedelta(minutes=grace_minutes)
        for complaint in db.list_complaints():
            if (complaint.get("kind") != "airline"
                    or complaint.get("status") != "accepted_pending_reference"
                    or complaint.get("reference")
                    or (complaint.get("flight_data") or {}).get("airline_code") != "SV"):
                continue
            created = parse_flight_time(complaint.get("created_at"))
            if not created or created > cutoff:
                continue
            key = f"telegram-reference-requested:{complaint['id']}"
            if db.event_seen(key):
                continue
            label = next(iter(
                self._complaint_flight_numbers(complaint)), "your Saudia flight")
            self.notify(
                f"I checked email first for the Saudia complaint on {label}, but "
                "no complaint reference arrived. Please reply with the SMS or "
                "paste its text here. I will extract and store only the complaint "
                "reference or service-ticket number; the "
                "seven-day GACA countdown remains based on the original "
                "submission time.",
                force_reply=True)
            db.mark_event_seen(key)

    def _handle_callback(self, callback: dict):
        data = callback.get("data") or ""
        self.api.answer_callback(callback["id"])
        if data.startswith("verify:"):
            with self._lock:
                waiter = self._verification
            if waiter:
                waiter.response = data.split(":", 1)[1]
                waiter.event.set()
            return
        if data.startswith("ticket_confirm:"):
            self._confirm_ticket_import(data.split(":", 1)[1])
            return
        if data.startswith("ticket_cancel:"):
            token = data.split(":", 1)[1]
            draft = db.get_ticket_import(token)
            if draft and str(draft.get("chat_id")) == self.chat_id:
                db.update_ticket_import(token, status="cancelled")
                self.notify("Cancelled that ticket import. Nothing was added.")
            return
        action, _, value = data.partition(":")
        if not value.isdigit():
            return
        flight = db.get_flight(int(value))
        if not flight:
            self.notify("That flight is no longer available.")
            return
        survey = db.survey_for_flight(flight["flight_key"])
        if (action in {"flight_good", "flight_no_issue", "flight_issue"}
                and survey and survey.get("status") == "retracted"):
            self.notify(
                "That check-in was retracted because the cancellation signal "
                "belonged to an earlier flight on the same booking. No complaint "
                "was opened from it.")
            return
        if action == "flight_good":
            db.update_survey_status(flight["flight_key"], "good")
            self.notify(f"Glad {self._flight_label(flight)} went well ✈️")
        elif action == "flight_no_issue":
            db.update_survey_status(flight["flight_key"], "no_issue")
            self.notify(
                "Understood—I will not open a complaint for this cancellation.")
        elif action == "flight_issue":
            prompt = self.notify(
                f"What went wrong on {self._flight_label(flight)}? Send a message, photos with a caption, or both. I’ll automatically file after the last message.",
                force_reply=True)
            db.record_survey(flight["flight_key"], self.chat_id,
                             prompt["message_id"], "awaiting_details")
        elif action == "escalate":
            self._launch_gaca(flight)
        elif action == "reopen_case":
            self._start_reopen_intake(flight)
        elif action == "close_case":
            airline = self._latest_airline_complaint(flight)
            if airline:
                db.close_complaint(airline["id"], "closed")
                db.clear_event_seen(f"airline-responded:{airline['id']}")
                db.clear_event_seen(f"auto-gaca:{airline['id']}")
            db.mark_event_seen(f"closed:{flight['flight_key']}")
            self.notify(
                "Case kept closed. I won’t escalate it to GACA. You can still "
                "reopen a fresh airline complaint later if needed.")
        elif action == "submit_issue":
            self._finalize_intake(flight["flight_key"])

    def _start_reopen_intake(self, flight: dict) -> None:
        """Allow a new airline filing after a closed/resolved cycle."""
        airline = self._latest_airline_complaint(flight)
        if airline:
            db.close_complaint(airline["id"], "closed")
            db.clear_event_seen(f"airline-responded:{airline['id']}")
            db.clear_event_seen(f"auto-gaca:{airline['id']}")
            db.clear_event_seen(
                f"auto-gaca-waiting-reference:{airline['id']}")
        db.clear_event_seen(f"closed:{flight['flight_key']}")
        with self._lock:
            previous = self._intakes.pop(flight["flight_key"], None)
            if previous and previous.timer:
                previous.timer.cancel()
            self._intakes[flight["flight_key"]] = PendingIntake(
                flight_key=flight["flight_key"],
                parent_complaint_id=(int(airline["id"]) if airline else None),
            )
        prompt = self.notify(
            f"Reopening the airline complaint for {self._flight_label(flight)}. "
            "Tell me what is still unresolved and send any photos. I will file "
            "a fresh complaint after the last message.",
            force_reply=True)
        db.record_survey(flight["flight_key"], self.chat_id,
                         prompt["message_id"], "awaiting_details")

    def _flight_label(self, flight: dict) -> str:
        number = flight.get("flight_number") or ", ".join(
            flight.get("flight_numbers") or []) or "your flight"
        route = " → ".join(filter(None, (
            flight.get("origin"), flight.get("destination"))))
        return f"{number} {route}".strip()

    def _post_flight_label(self, flight: dict) -> str:
        number = flight.get("flight_number") or ", ".join(
            flight.get("flight_numbers") or []) or ""
        origin = flight.get("origin") or "your origin"
        destination = flight.get("destination") or "your destination"
        prefix = f" {number}" if number else ""
        passenger = ((flight.get("overrides") or {}).get("passenger")
                     or flight.get("passenger") or "")
        passenger = re.sub(r"\s+e[\s-]*ticket\b.*$", "", passenger,
                           flags=re.IGNORECASE).strip()
        owner = self.config.get("user") or {}
        owner_names = {
            passenger_profile_key(owner.get("full_name") or ""),
            passenger_profile_key(" ".join(filter(None, (
                owner.get("first_name"), owner.get("middle_name"),
                owner.get("last_name"),
            )))),
        }
        booking_key = passenger_profile_key(passenger)
        owner_first = passenger_profile_key(owner.get("first_name") or "")
        is_owner = (not booking_key or booking_key in owner_names
                    or (len(booking_key.split()) == 1
                        and booking_key == owner_first))
        subject = "your flight" if is_owner else f"{passenger}'s flight"
        return f"{subject}{prefix} from {origin} to {destination}"

    def _passenger_profile_buttons(self, payload: dict, flight: dict) -> dict | None:
        """Create a signed mobile link without exposing another person's data."""
        passenger = payload.get("profile_passenger_name") or ""
        settings = self.config.get("web") or {}
        base_url = str(settings.get("public_base_url") or "").rstrip("/")
        secret = str(settings.get("access_secret") or "")
        if not passenger or not base_url or not secret:
            return None
        token = create_web_token(secret, self.chat_id)
        next_path = f"/flight/{flight['id']}/complaint/airline"
        query = urlencode({
            "passenger": passenger,
            "next": next_path,
            "access": token,
        })
        return {"inline_keyboard": [[{
            "text": f"Complete {passenger}'s profile",
            "url": f"{base_url}/settings/profile?{query}",
        }], [{
            "text": "Try filing again after saving",
            "callback_data": f"submit_issue:{flight['id']}",
        }]]}

    def _send_web_link(self):
        settings = self.config.get("web") or {}
        base_url = str(settings.get("public_base_url") or "").rstrip("/")
        secret = str(settings.get("access_secret") or "")
        if not base_url or not secret:
            self.notify("The private web link is not configured on this server yet.")
            return
        token = create_web_token(secret, self.chat_id)
        link = f"{base_url}/?access={token}"
        minutes = int(settings.get("link_expiry_minutes", 15))
        self.notify(
            f"Your private FlightDeck link is ready. This sign-in link expires in {minutes} minutes; the phone session stays signed in.",
            buttons={"inline_keyboard": [[{
                "text": "Open FlightDeck", "url": link,
            }]]})

    def _photo_path(self, flight_key: str) -> Path:
        folder = hashlib.sha256(flight_key.encode()).hexdigest()[:12]
        return TELEGRAM_EVIDENCE_DIR / folder / f"{uuid.uuid4().hex}.jpg"

    def _collect_issue(self, survey: dict, message: dict):
        flight_key = survey["flight_key"]
        with self._lock:
            intake = self._intakes.setdefault(
                flight_key, PendingIntake(flight_key=flight_key))
            value = (message.get("text") or message.get("caption") or "").strip()
            if value and not value.startswith("/"):
                intake.incident = (intake.incident + " " + value).strip()
            photos = message.get("photo") or []
            if photos:
                destination = self._photo_path(flight_key)
                self.api.download(photos[-1]["file_id"], destination)
                intake.attachments.append(str(destination))
            if intake.timer:
                intake.timer.cancel()
            delay = max(3, int(self.settings.get("complaint_debounce_seconds", 20)))
            intake.timer = threading.Timer(
                delay, self._finalize_intake, args=(flight_key,))
            intake.timer.daemon = True
            intake.timer.start()
        db.update_survey_status(flight_key, "collecting")
        flight = db.get_flight_by_key(flight_key)
        self.notify(
            "Got it. Send any more photos now; I’ll file automatically in "
            f"{delay} seconds after the last message.",
            buttons=_buttons([[('File now', f"submit_issue:{flight['id']}")]])
            if flight else None)

    def _cancel_latest_intake(self):
        with self._lock:
            for intake in self._intakes.values():
                if intake.timer:
                    intake.timer.cancel()
            self._intakes.clear()

    def _finalize_intake(self, flight_key: str):
        with self._lock:
            intake = self._intakes.pop(flight_key, None)
        if not intake:
            return
        if intake.timer:
            intake.timer.cancel()
        flight = db.get_flight_by_key(flight_key)
        if not flight:
            self.notify("The flight record disappeared before filing.")
            return
        if len(intake.incident.strip()) < 15:
            self.notify("I saved the photos, but need a short description of what went wrong.")
            with self._lock:
                self._intakes[flight_key] = intake
            db.update_survey_status(flight_key, "awaiting_details")
            return
        strategy = self._strategy_for_flight(flight)
        status_context = strategy.get("status") or {}
        rights_context = strategy.get("rights") or {}
        case_context = {
            "status": status_context.get("status"),
            "status_confidence": status_context.get("confidence"),
            "status_provider": status_context.get("provider"),
            "actual_arrival": status_context.get("actual_arrival"),
            "rights_verdict": rights_context.get("verdict"),
            "rights_reasons": rights_context.get("reasons"),
            "recommended_action": strategy.get("recommended_action"),
            "missing_facts": strategy.get("missing_facts"),
            "requested_remedy": strategy.get("requested_remedy"),
        }
        ai_analysis = None
        if (self.ai.enabled
                and hasattr(self.ai, "analyze_incident")
                and self.ai.settings.get("analyze_incidents", True)):
            self.notify(f"{self.ai.name} is organizing the issue and checking the safest next stepâ€¦")
            ai_analysis = self.ai.analyze_incident(
                intake.incident, flight, intake.attachments,
                case_context=case_context)
            if ai_analysis is None:
                reason = self.ai.last_error or "AI request unavailable"
                self.notify(
                    f"{self.ai.name} could not analyze this issue ({reason}). "
                    "I am continuing with the original statement and deterministic portal automation.")
        try:
            payload = complaint_payload(
                flight, self.config["user"], "airline", intake.incident,
                attachments=intake.attachments, ai_analysis=ai_analysis,
                passenger_profiles=self.config.get("passengers") or {})
        except ValueError as exc:
            self.notify(str(exc))
            return
        missing = missing_portal_fields(payload)
        if missing:
            db.update_survey_status(flight_key, "needs_profile")
            with self._lock:
                self._intakes[flight_key] = intake
            if payload.get("passenger_profile_missing"):
                passenger = payload.get("profile_passenger_name")
                found = db.identity_suggestions(passenger).get("values") or {}
                found_text = ""
                if found:
                    labels = ", ".join(sorted(
                        field.replace("_", " ") for field in found))
                    found_text = (f" I already found these labeled fields in "
                                  f"matching ticket evidence: {labels}.")
                ai_text = (f" {self.ai.name} will check the remaining scoped "
                           "ticket/PDF evidence when you open the profile."
                           if self.ai.enabled and self.ai.settings.get(
                               "extract_profile_evidence", True) else "")
                self.notify(
                    f"This booking belongs to {passenger}, not the account "
                    "owner. I stopped before submission so I do not reuse "
                    "Mansour's National ID or AlFursan number." + found_text + ai_text
                    + " Save this "
                    "passenger's identity once, then tap Try filing again.",
                    buttons=self._passenger_profile_buttons(payload, flight))
            else:
                self.notify("I need these one-time profile/flight details before filing: "
                            + ", ".join(missing) + ". Complete them in FlightDeck.")
            return
        complaint_id = db.begin_complaint(
            flight_key, "airline", payload["subject"], intake.incident,
            intake.attachments,
            submitted_text=payload.get("description") or "",
            parent_complaint_id=intake.parent_complaint_id,
            issue_summary=str(
                (ai_analysis or {}).get("summary") or intake.incident),
            requested_resolution_summary=str(
                (ai_analysis or {}).get("requested_remedy") or
                strategy.get("requested_remedy") or ""),
        )
        if complaint_id is None:
            existing = db.active_complaint_for_flight(flight_key, "airline")
            if existing and existing.get("status") in {
                    "submitted", "filed", "sent",
                    "accepted_pending_reference"}:
                db.update_survey_status(flight_key, "filed")
                self.notify(
                    "This flight already has an airline complaint on record. "
                    "I will not submit it again.")
            else:
                db.update_survey_status(flight_key, "filing")
                self.notify(
                    "A complaint for this flight is already being filed. "
                    "I will not start a duplicate job.")
            return
        payload["portal_complaint_id"] = complaint_id
        db.update_survey_status(flight_key, "filing")
        self.notify(f"Filing with {payload['airline_name']} on its official website now…")

        def finish_record(status: str, reference_value: str | None = None):
            db.finish_complaint(
                complaint_id, status, reference_value,
                submitted_text=payload.get("description") or None,
                portal_category=(
                    payload.get("selected_complaint_category") or None))

        def complete(result: PortalResult):
            if result.status == "submitted":
                finish_record("submitted", result.reference or None)
                db.update_survey_status(flight_key, "filed")
                reference = f" Reference: {result.reference}." if result.reference else ""
                self.notify("Complaint submitted on the official airline portal."
                            + reference + " I’ll watch for the airline’s response.")
            elif result.status == "accepted_pending_reference":
                finish_record("accepted_pending_reference")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(
                    "Saudia accepted the complaint without returning its "
                    "reference on the page. I will check email first; if the "
                    "reference is still missing after the mailbox scan, I will "
                    "ask you for the SMS in Telegram. I will not submit a duplicate.")
            elif result.status == "confirmation_unknown":
                finish_record("failed")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(
                    "The airline did not return readable confirmation. The "
                    "attempt is recorded as failed, not submitted.")
            else:
                finish_record("failed")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(f"Portal filing needs attention: {result.message}")

        start_portal_job(
            payload, on_complete=complete,
            on_update=self.portal_progress_handler())

    def _latest_airline_complaint(self, flight: dict) -> dict | None:
        preferred = None
        for item in reversed(flight.get("complaints") or []):
            if item.get("kind") != "airline":
                continue
            if item.get("status") == "submitted" and item.get("reference"):
                return item
            if (preferred is None
                    and item.get("reference")
                    and item.get("status") in {
                        "submitted", "accepted_pending_reference",
                        "closed", "resolved"}):
                preferred = item
        return preferred

    @staticmethod
    def _followup_incident(prior: dict, analysis: dict | None) -> str:
        original = str(
            prior.get("original_text") or prior.get("details") or ""
        ).strip()
        reference = str(prior.get("reference") or "").strip()
        response_summary = str(
            (analysis or {}).get("summary") or ""
        ).strip()
        context = (
            f"I previously raised this issue with the airline under reference "
            f"{reference}. " if reference else
            "I previously raised this issue with the airline. "
        )
        if response_summary:
            context += response_summary.rstrip(".") + ". "
        context += (
            "The complaint was closed without a satisfactory solution, and "
            "the original issue and requested resolution remain unresolved."
        )
        return "\n\n".join(part for part in (original, context) if part)

    def _launch_airline_followup(
            self,
            flight: dict,
            prior: dict,
            response_analysis: dict | None = None) -> bool:
        """Open one reference-aware airline child, then escalate its parent."""
        siblings = db.complaints_for_flight(flight["flight_key"])
        existing = next((
            item for item in siblings
            if item.get("kind") == "airline"
            and int(item.get("parent_complaint_id") or 0) == int(prior["id"])
            and item.get("status") in {
                "filing", "submitted", "filed", "sent",
                "accepted_pending_reference",
            }
        ), None)
        if existing:
            return True

        incident = self._followup_incident(prior, response_analysis)
        strategy = self._strategy_for_flight(flight)
        ai_analysis = None
        if (self.ai.enabled
                and hasattr(self.ai, "analyze_incident")
                and self.ai.settings.get("analyze_incidents", True)):
            try:
                ai_analysis = self.ai.analyze_incident(
                    incident,
                    flight,
                    prior.get("attachments") or [],
                    case_context={
                        "recommended_action": "reopen_airline_then_escalate",
                        "requested_remedy": strategy.get("requested_remedy"),
                    },
                )
            except Exception:
                logger.exception(
                    "Ghala could not prepare the airline follow-up text")
        try:
            payload = complaint_payload(
                flight,
                self.config["user"],
                "airline",
                incident,
                attachments=prior.get("attachments") or [],
                ai_analysis=ai_analysis,
                passenger_profiles=self.config.get("passengers") or {},
            )
        except ValueError as exc:
            self.notify(f"Could not prepare the airline follow-up: {exc}")
            return False
        missing = missing_portal_fields(payload)
        if missing:
            self.notify(
                "The airline follow-up is queued conceptually, but the saved "
                "profile still needs: " + ", ".join(missing) + ".")
            return False

        db.close_complaint(int(prior["id"]), "closed")
        complaint_id = db.begin_complaint(
            flight["flight_key"],
            "airline",
            payload["subject"],
            incident,
            prior.get("attachments") or [],
            submitted_text=payload.get("description") or "",
            parent_complaint_id=int(prior["id"]),
            issue_summary=str(
                (ai_analysis or {}).get("summary") or incident),
            requested_resolution_summary=str(
                (ai_analysis or {}).get("requested_remedy") or
                strategy.get("requested_remedy") or ""),
            escalate_parent_on_success=True,
        )
        if complaint_id is None:
            return False
        payload.update(
            portal_complaint_id=complaint_id,
            parent_complaint_id=int(prior["id"]),
            followup_then_gaca=True,
        )
        self.notify(
            f"The airline response to {prior.get('reference') or 'the prior case'} "
            "was not satisfactory. I am opening one reference-aware airline "
            "follow-up now; after it is accepted, I will escalate the original "
            "case to GACA automatically.")

        def finish_record(status: str, reference_value: str | None = None):
            db.finish_complaint(
                complaint_id,
                status,
                reference_value,
                submitted_text=payload.get("description") or None,
                portal_category=(
                    payload.get("selected_complaint_category") or None),
            )

        def complete(result: PortalResult):
            if result.status == "submitted":
                finish_record("submitted", result.reference or None)
                self.notify(
                    "The airline follow-up was submitted."
                    + (f" Reference: {result.reference}."
                       if result.reference else ""))
                self.resume_pending_parent_escalations()
            elif result.status == "accepted_pending_reference":
                finish_record("accepted_pending_reference")
                self.notify(
                    "The airline accepted the follow-up; its reference is "
                    "still being recovered from email/SMS. I am continuing "
                    "with the original case's GACA escalation.")
                self.resume_pending_parent_escalations()
            else:
                finish_record("failed")
                self.notify(
                    "The airline follow-up needs attention: "
                    f"{result.message}")

        start_portal_job(
            payload,
            on_complete=complete,
            on_update=self.portal_progress_handler(),
        )
        return True

    def resume_pending_parent_escalations(self) -> None:
        """Continue the durable provider-child → regulator-parent sequence."""
        for child in db.pending_parent_escalations():
            parent = db.get_complaint(int(child["parent_complaint_id"]))
            if not parent or not parent.get("reference"):
                continue
            flight = db.get_flight_by_key(child["flight_key"])
            if not flight:
                continue
            root_id = int(parent.get("root_complaint_id") or parent["id"])
            related_gaca = next((
                item for item in db.complaints_for_flight(child["flight_key"])
                if item.get("kind") == "gaca"
                and int(item.get("root_complaint_id") or item["id"]) == root_id
                and item.get("status") in {
                    "filing", "submitted", "filed", "sent",
                    "accepted_pending_reference",
                }
            ), None)
            if related_gaca:
                db.clear_parent_escalation_flag(int(child["id"]))
                continue
            if self._launch_gaca(
                    flight,
                    incident_suffix=(
                        "The airline has also accepted a new follow-up "
                        + (f"under reference {child['reference']}. "
                           if child.get("reference") else "")
                        + "because the original complaint was closed without "
                          "a satisfactory solution."
                    ),
                    prior_complaint=parent):
                db.clear_parent_escalation_flag(int(child["id"]))

    def _launch_gaca(self, flight: dict, incident_suffix: str = "",
                     automatic: bool = False,
                     prior_complaint: dict | None = None) -> bool:
        prior = prior_complaint or self._latest_airline_complaint(flight)
        if not prior or not prior.get("reference"):
            self.notify("GACA requires the airline complaint reference, which has not been captured yet.")
            return False
        now = self._flight_local_now()
        incident_day = parse_flight_time(
            self._effective_flight_value(flight, "flight_date")
        )
        if incident_day and (now.date() - incident_day.date()).days > 60:
            marker = f"gaca-blocked-60days:{prior['id']}"
            if not db.event_seen(marker):
                self.notify(
                    "This incident is more than 60 days old, so FlightDeck "
                    "will not send a GACA form that the official portal will "
                    "reject."
                )
                db.mark_event_seen(marker)
            return False
        root_id = int(prior.get("root_complaint_id") or prior["id"])
        confirmation_unknown = db.gaca_confirmation_unknown_complaint_ids()
        ambiguous = next((
            item
            for item in reversed(
                db.complaints_for_flight(flight["flight_key"])
            )
            if item.get("kind") == "gaca"
            and int(item["id"]) in confirmation_unknown
            and int(item.get("root_complaint_id") or item["id"]) == root_id
        ), None)
        if ambiguous:
            marker = f"gaca-awaiting-confirmation:{ambiguous['id']}"
            if not db.event_seen(marker):
                self.notify(
                    "A GACA form for this issue was already sent once, but "
                    "the portal did not reveal its reference. I am monitoring "
                    "SMS and email for the regulator reference and will not "
                    "risk a duplicate submission.")
                db.mark_event_seen(marker)
            return False
        try:
            delay_days = max(1, int(self.settings.get(
                "gaca_auto_escalate_days", 7)))
        except (TypeError, ValueError):
            delay_days = 7
        created = parse_flight_time(prior.get("created_at"))
        due = created + timedelta(days=delay_days) if created else None
        if not due or now < due:
            marker = f"gaca-waiting-period:{prior['id']}"
            if not db.event_seen(marker):
                due_text = (
                    due.strftime("%Y-%m-%d %H:%M")
                    if due else "after the airline filing date is verified"
                )
                self.notify(
                    f"GACA's {delay_days}-day airline handling window has not "
                    f"finished yet. This escalation is held safely until "
                    f"{due_text}; no premature complaint will be submitted.")
                db.mark_event_seen(marker)
            return False
        db.clear_event_seen(f"gaca-waiting-period:{prior['id']}")
        incident = prior.get("details") or "The airline response was unsatisfactory."
        if incident_suffix:
            incident = incident.rstrip() + "\n\n" + incident_suffix.strip()
        ai_analysis = None
        strategy = self._strategy_for_flight(flight)
        status_context = strategy.get("status") or {}
        rights_context = strategy.get("rights") or {}
        if (self.ai.enabled
                and self.ai.settings.get("analyze_incidents", True)):
            try:
                ai_analysis = self.ai.analyze_incident(
                    incident, flight, prior.get("attachments") or [],
                    case_context={
                        "status": status_context.get("status"),
                        "status_confidence": status_context.get("confidence"),
                        "status_provider": status_context.get("provider"),
                        "actual_arrival": status_context.get("actual_arrival"),
                        "rights_verdict": rights_context.get("verdict"),
                        "rights_reasons": rights_context.get("reasons"),
                        "recommended_action": strategy.get("recommended_action"),
                        "missing_facts": strategy.get("missing_facts"),
                        "requested_remedy": strategy.get("requested_remedy"),
                        "portal_destination": "gaca",
                        "exclude_structured_form_fields": True,
                    })
            except Exception:
                logger.exception(
                    "Ghala incident analysis failed during GACA filing")
                ai_analysis = None
            if ai_analysis is None:
                reason = self.ai.last_error or "AI request unavailable"
                self.notify(
                    f"{self.ai.name} could not analyze this escalation "
                    f"({reason}). I am continuing with the saved complaint "
                    "text and deterministic portal automation.")
        try:
            payload = complaint_payload(
                flight, self.config["user"], "gaca", incident,
                prior["reference"], (prior.get("created_at") or "")[:10],
                attachments=prior.get("attachments") or [],
                ai_analysis=ai_analysis,
                passenger_profiles=self.config.get("passengers") or {})
        except ValueError as exc:
            self.notify(str(exc))
            return False
        missing = missing_portal_fields(payload)
        if missing:
            self.notify("GACA filing still needs: " + ", ".join(missing) + ".")
            return False
        complaint_id = db.begin_complaint(
            flight["flight_key"], "gaca", payload["subject"], incident,
            prior.get("attachments") or [],
            submitted_text=payload.get("description") or "",
            parent_complaint_id=int(prior["id"]),
            issue_summary=str(
                (ai_analysis or {}).get("summary") or incident),
            requested_resolution_summary=str(
                (ai_analysis or {}).get("requested_remedy") or
                strategy.get("requested_remedy") or ""),
        )
        if complaint_id is None:
            self.notify(
                "A GACA escalation for this flight is already underway or on "
                "record. I will not submit it again.")
            return False
        airline_id = prior["id"]
        auto_key = f"auto-gaca:{airline_id}"
        inflight_key = f"auto-gaca-inflight:{airline_id}"
        payload.update(
            portal_complaint_id=complaint_id,
            portal_auto_key=auto_key if automatic else "",
            portal_inflight_key=inflight_key if automatic else "",
        )
        if automatic:
            # Prevent duplicate auto launches while the portal job runs; the
            # durable auto-gaca marker is written only after a real success.
            db.mark_event_seen(inflight_key)
            self.notify(
                "Seven days have passed without a substantive airline response. "
                "I am automatically escalating this complaint through GACA's "
                "official E-Services portal now.")
        else:
            self.notify("Escalating to GACA's official E-Services portal now...")

        def finish_record(status: str, reference_value: str | None = None):
            db.finish_complaint(
                complaint_id, status, reference_value,
                submitted_text=payload.get("description") or None,
                portal_category=(
                    payload.get("selected_complaint_category") or None))

        def complete(result: PortalResult):
            db.clear_event_seen(inflight_key)
            message = str(result.message or "")
            hard_block = bool(re.search(
                r"blocked the VPS browser|this page can'?t be displayed|"
                r"more than 60 days|incident id\s*:|"
                r"contact support for additional information",
                message, re.I))
            if result.status == "submitted":
                finish_record("submitted", result.reference or None)
                if automatic:
                    db.mark_event_seen(auto_key)
                suffix = f" Reference: {result.reference}." if result.reference else ""
                self.notify("GACA escalation submitted." + suffix)
            elif result.status == "accepted_pending_reference":
                finish_record("accepted_pending_reference")
                if automatic:
                    db.mark_event_seen(auto_key)
                self.notify(
                    "GACA accepted the escalation. Its confirmation page did "
                    "not show the regulator reference, so FlightDeck is waiting "
                    "for the matching email or SMS and will not submit it again.")
            elif result.status == "confirmation_unknown":
                finish_record("failed")
                db.clear_event_seen(auto_key)
                self.notify(
                    "GACA returned no readable confirmation. The escalation "
                    "attempt is recorded as failed, not submitted.")
            else:
                finish_record("needs_attention")
                if automatic and hard_block:
                    # WAF / 60-day portal rejection: do not auto-retry every
                    # monitor cycle (that created dozens of empty filings).
                    db.mark_event_seen(auto_key)
                    if re.search(r"more than 60 days", message, re.I):
                        db.mark_event_seen(f"gaca-blocked-60days:{airline_id}")
                else:
                    db.clear_event_seen(auto_key)
                self.notify(f"GACA escalation needs attention: {result.message}")

        start_portal_job(
            payload, on_complete=complete,
            on_update=self.portal_progress_handler())
        return True

    def auto_escalate_due_complaints(self, now: datetime | None = None):
        """File one GACA escalation after seven days without a real response."""
        now = now or datetime.now()
        try:
            delay_days = max(1, int(self.settings.get(
                "gaca_auto_escalate_days", 7)))
        except (TypeError, ValueError):
            delay_days = 7
        cutoff = now - timedelta(days=delay_days)
        for complaint in db.list_complaints():
            if (complaint.get("kind") != "airline"
                    or complaint.get("status") not in {
                        "submitted", "accepted_pending_reference"}):
                continue
            complaint_id = complaint["id"]
            scheduled_key = f"auto-gaca:{complaint_id}"
            inflight_key = f"auto-gaca-inflight:{complaint_id}"
            if (db.event_seen(scheduled_key)
                    or db.event_seen(inflight_key)
                    or db.event_seen(f"gaca-blocked-60days:{complaint_id}")
                    or db.event_seen(f"airline-responded:{complaint_id}")):
                continue
            created = parse_flight_time(complaint.get("created_at"))
            if not created or created > cutoff:
                continue
            if not complaint.get("reference"):
                waiting_key = f"auto-gaca-waiting-reference:{complaint_id}"
                if not db.event_seen(waiting_key):
                    scan_minutes = max(1, int(self.settings.get(
                        "mailbox_scan_minutes", 10)))
                    self.notify(
                        "GACA escalation is now due, but GACA requires the "
                        "airline complaint number. I am continuing to scan "
                        f"email every {scan_minutes} minutes and will launch "
                        "the escalation automatically as soon as the airline "
                        "reference is recovered.")
                    db.mark_event_seen(waiting_key)
                continue
            flight_id = complaint.get("flight_id")
            flight = db.get_flight(int(flight_id)) if flight_id else None
            if not flight:
                continue
            self._launch_gaca(
                flight,
                incident_suffix=(
                    f"Seven days have passed since the airline complaint was "
                    f"submitted on {(complaint.get('created_at') or '')[:10]}. "
                    "The airline did not provide a substantive response or "
                    "resolution within that period."),
                automatic=True)

    def _live_landed_cached(self, flight: dict) -> bool | None:
        key = flight.get("flight_key") or str(flight.get("id"))
        now = time.monotonic()
        poll = max(1, int((self.config.get("flight_status") or {}).get(
            "poll_minutes", 10))) * 60
        cached = self._status_cache.get(key)
        if cached and now - cached[0] < poll:
            return cached[1]
        try:
            value = live_landed(self.config, flight)
        except Exception:
            value = None
        self._status_cache[key] = (now, value)
        return value

    def refresh_watched_flights(self) -> None:
        """Refresh only near-term flights and announce exact-flight disruptions."""
        now = self._flight_local_now()
        for summary in db.list_flights():
            flight = db.get_flight(summary["id"]) or summary
            marker = (parse_flight_time(self._effective_flight_value(
                flight, "departure")) or parse_flight_time(
                    self._effective_flight_value(flight, "flight_date")))
            if not marker or not (now - timedelta(hours=18)
                                  <= marker <= now + timedelta(days=2)):
                continue
            key = flight.get("flight_key") or ""
            previous = db.get_flight_status_snapshot(key) or {}
            try:
                current = refresh_flight_status(
                    self.config, flight, now=self._flight_status_now(), force=False)
            except Exception:
                logger.exception("Flight status refresh failed for %s", key)
                continue
            status = current.get("status")
            if (status not in {"cancelled", "delayed", "diverted"}
                    or previous.get("status") == status):
                continue
            event_key = f"flight-status-alert:{key}:{status}"
            if db.event_seen(event_key):
                continue
            strategy = self._strategy_for_flight(flight)
            reason = (strategy.get("reasons") or [""])[0]
            self.notify(
                f"Flight update for {self._post_flight_label(flight)}: "
                f"{current.get('label') or status}. "
                f"Source: {current.get('provider') or 'unknown'} "
                f"({int(float(current.get('confidence') or 0) * 100)}% confidence).\n"
                f"Recommended next step: {strategy.get('label')}. {reason}")
            db.mark_event_seen(event_key)

    def _flight_local_now(self) -> datetime:
        """Return naive local wall time matching stored itinerary timestamps."""
        timezone_name = str(self.settings.get("timezone") or "Asia/Riyadh")
        try:
            return datetime.now(ZoneInfo(timezone_name)).replace(tzinfo=None)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown Telegram flight timezone %s; using server time",
                timezone_name)
            return datetime.now()

    def _flight_status_now(self) -> datetime:
        """Return an aware clock so provider timestamps remain genuine UTC."""
        timezone_name = str(self.settings.get("timezone") or "Asia/Riyadh")
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            return datetime.now().astimezone()

    def send_due_surveys(self, now: datetime | None = None):
        now = now or self._flight_local_now()
        delay = int(self.settings.get("post_flight_delay_minutes", 20))
        lookback = timedelta(hours=int(self.settings.get("survey_lookback_hours", 24)))
        for summary in db.list_flights():
            if db.survey_for_flight(summary["flight_key"]):
                continue
            flight = db.get_flight(summary["id"])
            cancelled = bool(self._effective_flight_value(
                flight, "cancelled"))
            arrival = (parse_flight_time((flight.get("overrides") or {}).get("actual_arrival"))
                       or parse_flight_time(flight.get("new_arrival"))
                       or parse_flight_time((flight.get("overrides") or {}).get("arrival"))
                       or parse_flight_time(flight.get("arrival")))
            if cancelled:
                scheduled = (arrival
                    or parse_flight_time(
                        (flight.get("overrides") or {}).get("departure"))
                    or parse_flight_time(flight.get("departure"))
                )
                if scheduled and scheduled < now - lookback:
                    continue
                message = self.notify(
                    f"I see {self._post_flight_label(flight)} was cancelled. "
                    "Were you rebooked or refunded? Tell me what happened and "
                    "send any screenshots or receipts—I can file the cancellation "
                    "complaint automatically.",
                    buttons=_buttons([
                        [("No complaint needed",
                          f"flight_no_issue:{flight['id']}")],
                        [("Report cancellation",
                          f"flight_issue:{flight['id']}")],
                    ]))
            else:
                if not arrival or arrival < now - lookback:
                    continue
                landed = self._live_landed_cached(flight)
                if landed is not True and not schedule_has_finished(
                        flight, delay_minutes=delay, now=now):
                    continue
                message = self.notify(
                    f"How was {self._post_flight_label(flight)}? If anything was broken, delayed, unavailable, or handled badly, tell me and send photos—I can file it automatically.",
                    buttons=_buttons([
                        [("Everything was good", f"flight_good:{flight['id']}")],
                        [("Report an issue", f"flight_issue:{flight['id']}")],
                    ]))
            db.record_survey(flight["flight_key"], self.chat_id,
                             message["message_id"], "asked")

    def _maybe_scan_mailbox(self, force: bool = False,
                            notify_when_done: bool = False) -> str:
        minutes = int(self.settings.get("mailbox_scan_minutes", 10))
        if ((minutes <= 0 and not force)
                or not (self.config.get("imap", {}).get("user")
                        and self.config.get("imap", {}).get("password"))):
            return "not_configured"
        if (not force
                and time.monotonic() - self._last_mail_scan < minutes * 60):
            return "throttled"
        if (self._mail_scan_thread
                and self._mail_scan_thread.is_alive()):
            return "running"
        self._last_mail_scan = time.monotonic()
        scan_config = json.loads(json.dumps(self.config))
        scan_config["imap"]["since_days"] = min(
            int(scan_config["imap"].get("since_days", 730)), 14)

        def run_scan():
            try:
                scan_mailbox(
                    scan_config, log=lambda *_args, **_kwargs: None)
                self._mail_scan_error = ""
                if notify_when_done:
                    counts = db.counts()
                    self.notify(
                        "Gmail incremental sync finished. FlightDeck now has "
                        f"{counts['emails']} parsed emails and "
                        f"{counts['flights']} flights.")
            except Exception as exc:
                self._mail_scan_error = type(exc).__name__
                logger.exception("Background incremental mailbox scan failed")
                if notify_when_done:
                    self.notify(
                        "Gmail incremental sync failed. The existing flights, "
                        "complaints, and email records were left unchanged.")

        self._mail_scan_thread = threading.Thread(
            target=run_scan, name="gmail-incremental-scan", daemon=True)
        self._mail_scan_thread.start()
        return "started"

    def check_complaint_responses(self):
        events = db.list_mail_events()
        reconcile_gaca_mail_events(events, notify=self.notify)
        substantive = re.compile(
            r"resolved|resolution|decision|outcome|approved|declined|denied|"
            r"refund|compensation|reimburse|closed|closure|processed|finalized|"
            r"تعويض|استرداد|مرفوض|إغلاق|حل|تم المعالجة",
            re.I)
        closure_notice = re.compile(
            r"\b(?:closed|closure|processed|finalized|ticket[\s-]*closed)\b|"
            r"إغلاق|تم المعالجة|تم الإغلاق|service ticket\s*[-–]?\s*closure",
            re.I)
        response_candidate = re.compile(
            r"review|regarding|with regard|update|decision|response|reply|"
            r"feedback|comment|request|ticket|case|complaint|claim|contacting|"
            r"inform|advise|resolved|resolution|approved|declined|refund|"
            r"compensation|closed|تعويض|استرداد|شكوى|طلب|رد|قرار",
            re.I)
        complaints = [
            complaint for complaint in db.list_complaints()
            if (complaint.get("kind") == "airline"
                and complaint.get("status") in {
                    "submitted", "accepted_pending_reference"})
        ]
        known_references = {
            str(complaint.get("reference") or "").strip().casefold()
            for complaint in db.list_complaints()
            if str(complaint.get("reference") or "").strip()
        }

        # First recover references from acknowledgement messages. Keep the
        # existing newest-pending-first behavior because a confirmation email
        # belongs to the complaint that has just been submitted, not to the
        # oldest ticket awaiting a later resolution.
        for complaint in complaints:
            if complaint.get("reference"):
                continue
            flight = complaint.get("flight_data") or {}
            info = AIRLINES.get(flight.get("airline_code"), {})
            domains = info.get("domains") or []
            created = parse_flight_time(complaint.get("created_at"))
            for event in events:
                capture_key = f"reference-captured:{event['id']}"
                if db.event_seen(capture_key):
                    continue
                event_date = parse_flight_time(event.get("date"))
                if created and event_date and event_date < created:
                    continue
                sender = parseaddr(event.get("sender") or "")[1].split(
                    "@")[-1].lower()
                if domains and not any(
                        sender == domain or sender.endswith("." + domain)
                        for domain in domains):
                    continue
                captured_reference = _airline_confirmation_reference(
                    event.get("subject") or "", event.get("body") or "")
                if not captured_reference:
                    continue
                if captured_reference.casefold() in known_references:
                    # A delayed/duplicated acknowledgement can be imported
                    # again after a restart. Never copy its already-owned
                    # reference onto another pending complaint.
                    db.mark_event_seen(capture_key)
                    continue
                db.finish_complaint(
                    complaint["id"], "submitted", captured_reference)
                db.mark_event_seen(capture_key)
                complaint["reference"] = captured_reference
                known_references.add(captured_reference.casefold())
                self.notify(
                    "Captured the airline complaint reference from its "
                    f"confirmation email: {captured_reference}.")
                break

        def event_is_from_airline(event: dict, complaint: dict) -> tuple[bool, dict]:
            flight = complaint.get("flight_data") or {}
            info = AIRLINES.get(flight.get("airline_code"), {})
            domains = info.get("domains") or []
            sender = parseaddr(event.get("sender") or "")[1].split(
                "@")[-1].lower()
            valid = (not domains or any(
                sender == domain or sender.endswith("." + domain)
                for domain in domains))
            return valid, info

        def case_fact_score(blob: str, complaint: dict) -> int:
            """Score deterministic booking facts; AI is not needed here."""
            flight = complaint.get("flight_data") or {}
            compact_blob = re.sub(r"[^a-z0-9]", "", blob.casefold())

            def compact(value) -> str:
                return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

            score = 0
            pnr = compact(flight.get("pnr"))
            if len(pnr) >= 5 and pnr in compact_blob:
                score += 100
            for ticket in flight.get("ticket_numbers") or []:
                token = compact(ticket)
                if len(token) >= 8 and token in compact_blob:
                    score += 100
                    break
            numbers = list(flight.get("flight_numbers") or [])
            if flight.get("flight_number"):
                numbers.append(flight["flight_number"])
            if any(len(compact(number)) >= 4
                   and compact(number) in compact_blob for number in numbers):
                score += 45
            passenger = passenger_profile_key(
                (flight.get("overrides") or {}).get("passenger")
                or flight.get("passenger") or "")
            name_parts = [part for part in passenger.split() if len(part) >= 3]
            blob_words = set(re.findall(r"[a-z0-9]+", blob.casefold()))
            if len(name_parts) >= 2 and all(
                    part in blob_words for part in name_parts):
                score += 60
            flight_date = compact(flight.get("flight_date"))
            if len(flight_date) == 8 and flight_date in compact_blob:
                score += 10
            return score

        def response_analysis(event: dict, complaint: dict,
                              info: dict) -> tuple[bool, dict | None]:
            blob = " ".join((event.get("subject") or "",
                             event.get("body") or ""))
            deterministic = bool(substantive.search(blob))
            is_closure = bool(closure_notice.search(blob))
            analysis = None
            # Only spend an AI call once the email is already strongly tied to
            # this complaint (exact reference or booking facts). That stops Ghala
            # from analyzing unrelated mail against the wrong open tickets.
            if (self.ai.enabled
                    and self.ai.settings.get("analyze_responses", True)
                    and (deterministic or response_candidate.search(blob))):
                flight = complaint.get("flight_data") or {}
                model = str(getattr(self.ai, "model", "") or "configured")
                cache_material = json.dumps({
                    "event_id": event.get("id"),
                    "complaint_id": complaint.get("id"),
                    "reference": complaint.get("reference") or "",
                    "airline": (info.get("name")
                                or flight.get("airline_name") or "Airline"),
                    "subject": event.get("subject") or "",
                    "body": event.get("body") or "",
                }, ensure_ascii=False, sort_keys=True)
                cache_key = "response-v2:" + hashlib.sha256(
                    cache_material.encode("utf-8")).hexdigest()
                analysis = db.get_ai_analysis_cache(cache_key, model)
                if analysis is None:
                    try:
                        analysis = self.ai.analyze_response(
                            event.get("subject") or "", event.get("body") or "",
                            complaint.get("reference") or "",
                            info.get("name") or flight.get("airline_name")
                            or "Airline")
                    except Exception:
                        logger.exception(
                            "Ghala response analysis failed; using deterministic rules")
                        analysis = None
                    if analysis is not None:
                        db.save_ai_analysis_cache(
                            cache_key, "airline_response", model, analysis)
            if analysis is not None:
                analysis = dict(analysis)
                actionable = (
                    bool(analysis.get("substantive"))
                    or bool(analysis.get("closed_needs_followup"))
                    or is_closure)
                if is_closure:
                    analysis["closed_needs_followup"] = True
                    analysis["substantive"] = True
                    if str(analysis.get("recommendation") or "").casefold() in {
                            "", "wait", "accept"}:
                        analysis["recommendation"] = "escalate"
                return actionable, analysis
            if is_closure:
                return True, {
                    "summary": "The airline closed or finalized this complaint.",
                    "outcome": "unknown",
                    "amounts_or_deadlines": [],
                    "recommendation": "escalate",
                    "rationale": (
                        "Closure/processed notices require a reopen or GACA "
                        "decision even when no remedy details are included."),
                    "substantive": True,
                    "closed_needs_followup": True,
                }
            return deterministic, None

        def notify_response(complaint: dict, info: dict, event: dict,
                            analysis: dict | None, match_method: str) -> None:
            flight = complaint.get("flight_data") or {}
            needs_followup = bool(
                (analysis or {}).get("closed_needs_followup"))
            if analysis:
                amounts = "; ".join(analysis.get("amounts_or_deadlines") or [])
                amount_line = f"\nAmounts/deadlines: {amounts}" if amounts else ""
                default_summary = (
                    "The airline closed this complaint without a clear remedy."
                    if needs_followup else
                    "A substantive response was received.")
                response_text = (
                    f"{analysis.get('summary') or default_summary}"
                    f"\nOutcome: {str(analysis.get('outcome') or 'unknown').replace('_', ' ')}"
                    f"{amount_line}\n{self.ai.name} recommends: "
                    f"{str(analysis.get('recommendation') or 'review').replace('_', ' ')}"
                    f" — {analysis.get('rationale') or 'Review the airline response.'}")
            else:
                response_text = _clean_excerpt(
                    event.get("body") or event.get("subject") or "")
            if match_method == "exact_reference":
                matched_by = "Matched by the complaint reference in the email."
            else:
                matched_by = (
                    "The email omitted the reference; I matched its booking facts "
                    "(such as PNR, ticket, flight, date, or passenger) in code.")
            prompt = (
                "The airline marked this ticket closed or processed. Do you want "
                "me to escalate to GACA or reopen a fresh airline complaint?"
                if needs_followup else
                "Do you want me to escalate this to GACA?")
            self.notify(
                f"{info.get('name') or 'The airline'} responded to complaint "
                f"{complaint.get('reference') or ''} for "
                f"{self._post_flight_label(flight)}.\n{matched_by}\n\n"
                f"{response_text}\n\n{prompt}",
                buttons=_buttons([
                    [("Escalate to GACA", f"escalate:{complaint['flight_id']}"),
                     ("Reopen with airline",
                      f"reopen_case:{complaint['flight_id']}")],
                    [("No, close", f"close_case:{complaint['flight_id']}")],
                ]))

        def retain_and_auto_handle(
                complaint: dict,
                event: dict,
                analysis: dict | None) -> None:
            raw_response = str(
                event.get("body") or event.get("subject") or "")
            db.set_complaint_response(
                int(complaint["id"]),
                response_text=raw_response,
                response_summary=str(
                    (analysis or {}).get("summary") or
                    _clean_excerpt(raw_response)),
            )
            recommendation = str(
                (analysis or {}).get("recommendation") or "").casefold()
            outcome = str(
                (analysis or {}).get("outcome") or "").casefold()
            unsatisfactory = (
                bool((analysis or {}).get("closed_needs_followup"))
                or recommendation in {"escalate", "reopen"}
                or outcome in {"declined", "partially_approved"}
            )
            if not unsatisfactory:
                return
            action_key = (
                f"auto-airline-followup:{complaint['id']}:{event['id']}")
            if db.event_seen(action_key):
                return
            db.mark_event_seen(action_key)
            flight_id = complaint.get("flight_id")
            flight = db.get_flight(int(flight_id)) if flight_id else None
            if not flight:
                return
            self._launch_airline_followup(
                flight, complaint, response_analysis=analysis)

        # Reference-bearing responses are authoritative. Process emails
        # chronologically for deterministic state. FIFO fallback is disabled:
        # only exact references and strong booking-fact matches may link mail.
        ordered_events = sorted(
            events, key=lambda event: (
                parse_flight_time(event.get("date")) or datetime.min,
                int(event.get("id") or 0)))
        ordered_complaints = sorted(
            complaints, key=lambda complaint: (
                parse_flight_time(complaint.get("created_at")) or datetime.min,
                int(complaint.get("id") or 0)))
        for event in ordered_events:
            blob = " ".join((event.get("subject") or "",
                             event.get("body") or ""))
            blob_folded = blob.casefold()
            for complaint in ordered_complaints:
                if (db.event_seen(f"airline-responded:{complaint['id']}")
                        or db.event_seen(f"closed:{complaint['flight_key']}")):
                    continue
                reference = str(complaint.get("reference") or "").casefold()
                if not reference or reference not in blob_folded:
                    continue
                created = parse_flight_time(complaint.get("created_at"))
                event_date = parse_flight_time(event.get("date"))
                if created and event_date and event_date < created:
                    continue
                valid_sender, info = event_is_from_airline(event, complaint)
                if not valid_sender:
                    continue
                is_substantive, analysis = response_analysis(
                    event, complaint, info)
                if not is_substantive:
                    continue
                if not db.link_complaint_response(
                        complaint["id"], event["id"], "exact_reference"):
                    break
                db.mark_event_seen(
                    f"complaint-response:{complaint['id']}:{event['id']}")
                db.mark_event_seen(f"airline-responded:{complaint['id']}")
                notify_response(
                    complaint, info, event, analysis, "exact_reference")
                retain_and_auto_handle(complaint, event, analysis)
                break

        known_references = {
            str(complaint.get("reference") or "").casefold()
            for complaint in ordered_complaints if complaint.get("reference")
        }
        for event in ordered_events:
            if int(event.get("id") or 0) <= self._fifo_response_floor:
                continue
            blob = " ".join((event.get("subject") or "",
                             event.get("body") or ""))
            blob_folded = blob.casefold()
            if any(reference in blob_folded for reference in known_references):
                continue
            if _airline_confirmation_reference(
                    event.get("subject") or "", event.get("body") or ""):
                continue

            # Strong booking-fact matches only (PNR/ticket, or flight+passenger).
            fact_matches = []
            for complaint in ordered_complaints:
                if (db.event_seen(f"airline-responded:{complaint['id']}")
                        or db.event_seen(f"closed:{complaint['flight_key']}")):
                    continue
                created = parse_flight_time(complaint.get("created_at"))
                event_date = parse_flight_time(event.get("date"))
                if created and event_date and event_date < created:
                    continue
                valid_sender, info = event_is_from_airline(event, complaint)
                if not valid_sender:
                    continue
                score = case_fact_score(blob, complaint)
                if score >= 100:
                    fact_matches.append((score, complaint, info))
            fact_matches.sort(key=lambda item: item[0], reverse=True)
            if not (fact_matches and (len(fact_matches) == 1
                                      or fact_matches[0][0] > fact_matches[1][0])):
                continue
            _score, complaint, info = fact_matches[0]
            is_substantive, analysis = response_analysis(
                event, complaint, info)
            if is_substantive and db.link_complaint_response(
                    complaint["id"], event["id"], "case_facts"):
                db.mark_event_seen(
                    f"complaint-response:{complaint['id']}:{event['id']}")
                db.mark_event_seen(
                    f"airline-responded:{complaint['id']}")
                notify_response(
                    complaint, info, event, analysis, "case_facts")
                retain_and_auto_handle(complaint, event, analysis)

_COORDINATOR: TelegramCoordinator | None = None
_COORDINATOR_LOCK = threading.Lock()


def start_telegram(config: dict) -> TelegramCoordinator | None:
    global _COORDINATOR
    settings = config.get("telegram") or {}
    if not (settings.get("enabled") and settings.get("bot_token")
            and settings.get("chat_id")):
        return None
    with _COORDINATOR_LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = TelegramCoordinator(config).start()
    return _COORDINATOR
