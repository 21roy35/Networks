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

import requests

from . import db
from .ai_assistant import ClaudeAssistant
from .airlines import AIRLINES
from .captcha_solver import TwoCaptchaSolver
from .complaints import complaint_payload, missing_portal_fields
from .config import TELEGRAM_EVIDENCE_DIR, passenger_profile_key
from .flight_status import live_landed, parse_flight_time, schedule_has_finished
from .pipeline import scan_mailbox
from .portal_automation import (PortalResult, _extract_reference, set_ai_handler,
                                set_captcha_solver, set_verification_handler,
                                start_portal_job)
from .web_access import create_web_token


logger = logging.getLogger(__name__)


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
    reference = _extract_reference(blob)
    if reference:
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


class TelegramAPI:
    def __init__(self, token: str, session=None):
        self.token = token
        self.session = session or requests.Session()
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method: str, data: dict | None = None, files=None,
             timeout: int = 35):
        response = self.session.post(
            self.base + method, data=data or {}, files=files, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or method)
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
        response.raise_for_status()
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
        self._ai_chat_lock = threading.Lock()
        self._ai_chat_thread: threading.Thread | None = None
        self._ai_context: dict[str, object] = {}
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
            self.captcha.solve_recaptcha if self.captcha.enabled else None)
        threading.Thread(
            target=self._register_commands, name="telegram-commands",
            daemon=True).start()
        threading.Thread(target=self._poll_loop, name="telegram-updates",
                         daemon=True).start()
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
        return self.api.send_message(
            self.chat_id, text, reply_markup=buttons, force_reply=force_reply)

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
        }
        terminal = {
            "submitted", "accepted_pending_reference", "confirmation_unknown",
            "needs_attention", "error",
        }

        def deliver():
            while True:
                status, text, image = deliveries.get()
                try:
                    if image:
                        self.api.send_photo(self.chat_id, image, text)
                    else:
                        self.api.send_message(self.chat_id, text)
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
                    or (status in terminal and previous == status)
                    or (status == previous and not image)):
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
            self.api.send_photo(self.chat_id, image, prompt, reply_markup=markup)
        else:
            self.notify(prompt, buttons=markup, force_reply=not choices)
        timeout = int(self.settings.get("verification_timeout_minutes", 10)) * 60
        waiter.event.wait(timeout)
        with self._lock:
            if self._verification is waiter:
                self._verification = None
        if not waiter.event.is_set():
            self.notify("Verification timed out. The portal submission was paused safely.")
            return None
        return waiter.response

    def _poll_loop(self):
        timeout = int(self.settings.get("poll_timeout_seconds", 25))
        while not self.stop_event.is_set():
            try:
                for update in self.api.updates(self.offset, timeout):
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    self.handle_update(update)
            except Exception:
                logger.exception("Telegram update polling failed")
                self.stop_event.wait(3)

    def _monitor_loop(self):
        interval = max(15, int(self.settings.get("monitor_interval_seconds", 60)))
        while not self.stop_event.is_set():
            try:
                self._maybe_scan_mailbox()
                self.send_due_surveys()
                self.check_complaint_responses()
                self.ask_for_pending_references()
                self.auto_escalate_due_complaints()
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

    def _handle_message(self, message: dict):
        if self._handle_verification_message(message):
            return
        text = (message.get("text") or "").strip()
        if text == "/start":
            self.notify(
                "FlightDeck Telegram is connected. I’ll check in after flights, collect issue photos, file official complaints, relay verification steps, and report airline responses. Use /status for service status or /web for your private dashboard.")
            return
        if text == "/status":
            self._send_status()
            return
        if text and text.split(maxsplit=1)[0].lower() == "/web":
            self._send_web_link()
            return
        if text == "/cancel":
            self._cancel_latest_intake()
            self.notify("Cancelled the pending complaint intake.")
            return

        pasted = (message.get("text") or message.get("caption") or "").strip()
        reference = _telegram_sms_reference(pasted)
        if reference and self._capture_telegram_reference(
                reference, pasted, message):
            return

        positive = re.fullmatch(
            r"(?:good|great|fine|perfect|all good|no issues?|it was good|"
            r"ممتاز|جيد|تمام|ما فيه مشاكل)[.! ]*", text, re.I)
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        survey = (db.pending_survey(self.chat_id, reply_id)
                  if reply_id is not None else None)
        if not survey:
            pending = db.pending_survey(self.chat_id)
            if (pending and (
                    pending.get("status") in {"awaiting_details", "collecting"}
                    or positive or message.get("photo")
                    or self._looks_like_survey_issue(pasted))):
                survey = pending
        if not survey:
            if pasted and self.ai.enabled:
                self._dispatch_ai_message(pasted)
            elif not pasted and message.get("photo"):
                self.notify(
                    "Add a caption telling me which flight or complaint this "
                    "photo belongs to.")
            else:
                self.notify(
                    "Reply to a post-flight question, use /status, or ask me "
                    "about a flight, passenger, complaint, email, or screenshot.")
            return
        if (survey.get("status") == "asked" and positive
                and not message.get("photo")):
            db.update_survey_status(survey["flight_key"], "good")
            self.notify("Glad the flight went well ✈️")
            return
        self._collect_issue(survey, message)

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
                ai_status += (" Guarded ticket/PDF review is enabled for "
                              "missing passenger-profile fields.")
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
        return (
            f"FlightDeck is running. {counts['flights']} flights, "
            f"{counts['complaints']} complaints, {counts['emails']} parsed emails."
            + mailbox_status + ai_status + captcha_status
            + f" GACA auto-escalation is on after {auto_days} days "
              "without a substantive airline response.")

    def _send_status(self):
        self.notify(self._status_text())

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
        flight_actions = {"flight_details", "list_flights"}
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
        flights = []
        for flight in db.list_flights()[:20]:
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
            })
        all_complaints = db.list_complaints()
        complaints = []
        for complaint in all_complaints[:20]:
            flight = complaint.get("flight_data") or {}
            complaints.append({
                "reference": complaint.get("reference") or "",
                "kind": complaint.get("kind") or "",
                "status": complaint.get("status") or "",
                "created_at": complaint.get("created_at") or "",
                "flight_number": self._effective_flight_value(
                    flight, "flight_number") or ", ".join(
                        flight.get("flight_numbers") or []),
                "passenger": self._flight_passenger(flight),
                "has_evidence": bool(complaint.get("attachments")),
            })
        screenshots = TELEGRAM_EVIDENCE_DIR / "portal_jobs"
        return {
            "counts": db.counts(),
            "mailbox": db.mailbox_cursor_summary(),
            "flights": flights,
            "complaints": complaints,
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
            f"Issue: {_clean_excerpt(complaint.get('details') or 'not recorded', 900)}",
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
                self.api.send_photo(
                    self.chat_id, path.read_bytes(),
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
                self.api.send_photo(
                    self.chat_id, path.read_bytes(),
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
        if name == "complaint_responses":
            return self._send_response_details(action)
        if name == "search_email":
            return self._send_email_search(action)
        if name == "show_evidence":
            return self._send_evidence(action)
        if name == "latest_screenshot":
            return self._send_latest_screenshots(action)
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
                "portal screenshots, status, or a fresh Gmail sync. Existing "
                "/status, /web, /cancel, post-flight, verification, and complaint "
                "flows keep priority.")
            return True
        return False

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
        action, _, value = data.partition(":")
        if not value.isdigit():
            return
        flight = db.get_flight(int(value))
        if not flight:
            self.notify("That flight is no longer available.")
            return
        if action == "flight_good":
            db.update_survey_status(flight["flight_key"], "good")
            self.notify(f"Glad {self._flight_label(flight)} went well ✈️")
        elif action == "flight_issue":
            prompt = self.notify(
                f"What went wrong on {self._flight_label(flight)}? Send a message, photos with a caption, or both. I’ll automatically file after the last message.",
                force_reply=True)
            db.record_survey(flight["flight_key"], self.chat_id,
                             prompt["message_id"], "awaiting_details")
        elif action == "escalate":
            self._launch_gaca(flight)
        elif action == "close_case":
            db.mark_event_seen(f"closed:{flight['flight_key']}")
            self.notify("Case kept closed. I won’t escalate it to GACA.")
        elif action == "submit_issue":
            self._finalize_intake(flight["flight_key"])

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
        ai_analysis = None
        if (self.ai.enabled
                and self.ai.settings.get("analyze_incidents", True)):
            self.notify(f"{self.ai.name} is organizing the issue and checking the safest next stepâ€¦")
            ai_analysis = self.ai.analyze_incident(
                intake.incident, flight, intake.attachments)
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
            intake.attachments)
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
        db.update_survey_status(flight_key, "filing")
        self.notify(f"Filing with {payload['airline_name']} on its official website now…")

        def complete(result: PortalResult):
            if result.status == "submitted":
                db.finish_complaint(
                    complaint_id, "submitted", result.reference or None)
                db.update_survey_status(flight_key, "filed")
                reference = f" Reference: {result.reference}." if result.reference else ""
                self.notify("Complaint submitted on the official airline portal."
                            + reference + " I’ll watch for the airline’s response.")
            elif result.status == "accepted_pending_reference":
                db.finish_complaint(
                    complaint_id, "accepted_pending_reference")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(
                    "Saudia accepted the complaint without returning its "
                    "reference on the page. I will check email first; if the "
                    "reference is still missing after the mailbox scan, I will "
                    "ask you for the SMS in Telegram. I will not submit a duplicate.")
            elif result.status == "confirmation_unknown":
                db.finish_complaint(complaint_id, "failed")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(
                    "The airline did not return readable confirmation. The "
                    "attempt is recorded as failed, not submitted.")
            else:
                db.finish_complaint(complaint_id, "failed")
                db.update_survey_status(flight_key, "needs_attention")
                self.notify(f"Portal filing needs attention: {result.message}")

        start_portal_job(
            payload, on_complete=complete,
            on_update=self.portal_progress_handler())

    def _latest_airline_complaint(self, flight: dict) -> dict | None:
        for item in reversed(flight.get("complaints") or []):
            if item.get("kind") == "airline" and item.get("status") == "submitted":
                return item
        return None

    def _launch_gaca(self, flight: dict, incident_suffix: str = "",
                     automatic: bool = False) -> bool:
        prior = self._latest_airline_complaint(flight)
        if not prior or not prior.get("reference"):
            self.notify("GACA requires the airline complaint reference, which has not been captured yet.")
            return False
        incident = prior.get("details") or "The airline response was unsatisfactory."
        if incident_suffix:
            incident = incident.rstrip() + "\n\n" + incident_suffix.strip()
        ai_analysis = None
        if (self.ai.enabled
                and self.ai.settings.get("analyze_incidents", True)):
            ai_analysis = self.ai.analyze_incident(
                incident, flight, prior.get("attachments") or [])
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
            prior.get("attachments") or [])
        if complaint_id is None:
            self.notify(
                "A GACA escalation for this flight is already underway or on "
                "record. I will not submit it again.")
            return False
        if automatic:
            self.notify(
                "Seven days have passed without a substantive airline response. "
                "I am automatically escalating this complaint through GACA's "
                "official E-Services portal now.")
        else:
            self.notify("Escalating to GACA's official E-Services portal now...")

        def complete(result: PortalResult):
            if result.status == "submitted":
                db.finish_complaint(
                    complaint_id, "submitted", result.reference or None)
                suffix = f" Reference: {result.reference}." if result.reference else ""
                self.notify("GACA escalation submitted." + suffix)
            elif result.status == "confirmation_unknown":
                db.finish_complaint(complaint_id, "failed")
                self.notify(
                    "GACA returned no readable confirmation. The escalation "
                    "attempt is recorded as failed, not submitted.")
            else:
                db.finish_complaint(complaint_id, "needs_attention")
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
            if (db.event_seen(scheduled_key)
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
            if self._launch_gaca(
                    flight,
                    incident_suffix=(
                        f"Seven days have passed since the airline complaint was "
                        f"submitted on {(complaint.get('created_at') or '')[:10]}. "
                        "The airline did not provide a substantive response or "
                        "resolution within that period."),
                    automatic=True):
                # One automatic attempt only. Any portal issue is surfaced for
                # human attention instead of risking duplicate submissions.
                db.mark_event_seen(scheduled_key)

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

    def send_due_surveys(self, now: datetime | None = None):
        now = now or datetime.now()
        delay = int(self.settings.get("post_flight_delay_minutes", 20))
        lookback = timedelta(hours=int(self.settings.get("survey_lookback_hours", 24)))
        for summary in db.list_flights():
            if db.survey_for_flight(summary["flight_key"]):
                continue
            flight = db.get_flight(summary["id"])
            arrival = (parse_flight_time((flight.get("overrides") or {}).get("actual_arrival"))
                       or parse_flight_time(flight.get("new_arrival"))
                       or parse_flight_time((flight.get("overrides") or {}).get("arrival"))
                       or parse_flight_time(flight.get("arrival")))
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
        substantive = re.compile(
            r"resolved|resolution|decision|outcome|approved|declined|denied|"
            r"refund|compensation|reimburse|closed|تعويض|استرداد|مرفوض|إغلاق|حل",
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
                db.finish_complaint(
                    complaint["id"], "submitted", captured_reference)
                db.mark_event_seen(capture_key)
                complaint["reference"] = captured_reference
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
            analysis = None
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
                cache_key = "response-v1:" + hashlib.sha256(
                    cache_material.encode("utf-8")).hexdigest()
                analysis = db.get_ai_analysis_cache(cache_key, model)
                if analysis is None:
                    analysis = self.ai.analyze_response(
                        event.get("subject") or "", event.get("body") or "",
                        complaint.get("reference") or "",
                        info.get("name") or flight.get("airline_name") or "Airline")
                    if analysis is not None:
                        db.save_ai_analysis_cache(
                            cache_key, "airline_response", model, analysis)
            if analysis is not None:
                return bool(analysis.get("substantive")), analysis
            return deterministic, None

        def notify_response(complaint: dict, info: dict, event: dict,
                            analysis: dict | None, match_method: str) -> None:
            flight = complaint.get("flight_data") or {}
            if analysis:
                amounts = "; ".join(analysis.get("amounts_or_deadlines") or [])
                amount_line = f"\nAmounts/deadlines: {amounts}" if amounts else ""
                response_text = (
                    f"{analysis.get('summary') or 'A substantive response was received.'}"
                    f"\nOutcome: {str(analysis.get('outcome') or 'unknown').replace('_', ' ')}"
                    f"{amount_line}\n{self.ai.name} recommends: "
                    f"{str(analysis.get('recommendation') or 'review').replace('_', ' ')}"
                    f" — {analysis.get('rationale') or 'Review the airline response.'}")
            else:
                response_text = _clean_excerpt(
                    event.get("body") or event.get("subject") or "")
            if match_method == "exact_reference":
                matched_by = "Matched by the complaint reference in the email."
            elif match_method == "case_facts":
                matched_by = (
                    "The email omitted the reference; I matched its booking facts "
                    "(such as PNR, ticket, flight, date, or passenger) in code.")
            else:
                matched_by = (
                    "The email omitted the reference, so I matched it to the "
                    "oldest unresolved ticket for this airline, preserving filing order.")
            self.notify(
                f"{info.get('name') or 'The airline'} responded to complaint "
                f"{complaint.get('reference') or ''} for "
                f"{self._post_flight_label(flight)}.\n{matched_by}\n\n"
                f"{response_text}\n\n"
                "Do you want me to escalate this to GACA?",
                buttons=_buttons([[
                    ("Escalate to GACA", f"escalate:{complaint['flight_id']}"),
                    ("No, close", f"close_case:{complaint['flight_id']}"),
                ]]))

        # Reference-bearing responses are authoritative and always win over
        # receipt order. Process emails chronologically for deterministic state.
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
                break

        # Some final-resolution templates omit the ticket number. Match those
        # messages FIFO within the airline, never globally, and persist the
        # assignment so restarts or rescans cannot reshuffle it.
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
            # A different explicit case reference must never consume our FIFO.
            if _airline_confirmation_reference(
                    event.get("subject") or "", event.get("body") or ""):
                continue

            # Prefer a unique deterministic booking-fact match over filing
            # order. This handles family passengers and multiple flights
            # without spending an AI call or trusting a probabilistic answer.
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
                if score >= 45:
                    fact_matches.append((score, complaint, info))
            fact_matches.sort(key=lambda item: item[0], reverse=True)
            if (fact_matches and (len(fact_matches) == 1
                                  or fact_matches[0][0] > fact_matches[1][0])):
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
                continue

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
                is_substantive, analysis = response_analysis(
                    event, complaint, info)
                if not is_substantive:
                    break
                if not db.link_complaint_response(
                        complaint["id"], event["id"], "fifo_airline"):
                    break
                db.mark_event_seen(
                    f"complaint-response:{complaint['id']}:{event['id']}")
                db.mark_event_seen(f"airline-responded:{complaint['id']}")
                notify_response(complaint, info, event, analysis, "fifo_airline")
                break

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
