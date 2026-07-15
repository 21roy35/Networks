"""Telegram-first orchestration for post-flight feedback and complaints."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

import requests

from . import db
from .ai_assistant import ClaudeAssistant
from .airlines import AIRLINES
from .captcha_solver import TwoCaptchaSolver
from .complaints import complaint_payload, missing_portal_fields
from .config import TELEGRAM_EVIDENCE_DIR
from .flight_status import live_landed, parse_flight_time, schedule_has_finished
from .pipeline import scan_mailbox
from .portal_automation import (PortalResult, _extract_reference, set_ai_handler,
                                set_captcha_solver, set_verification_handler,
                                start_portal_job)
from .web_access import create_web_token


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
        self._status_cache: dict[str, tuple[float, bool | None]] = {}

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
            pass

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
                    pass
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
                pass
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

    def _handle_message(self, message: dict):
        if self._handle_verification_message(message):
            return
        text = (message.get("text") or "").strip()
        if text == "/start":
            self.notify(
                "FlightDeck Telegram is connected. I’ll check in after flights, collect issue photos, file official complaints, relay verification steps, and report airline responses. Use /status for service status or /web for your private dashboard.")
            return
        if text == "/status":
            counts = db.counts()
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
            else:
                ai_status = " AI assistance is off."
            captcha_status = (" 2Captcha is configured with Telegram fallback."
                              if self.captcha.enabled
                              else " Automatic CAPTCHA solving is off.")
            self.notify(
                f"FlightDeck is running. {counts['flights']} flights, "
                f"{counts['complaints']} complaints, {counts['emails']} parsed emails."
                + ai_status + captcha_status
                + f" GACA auto-escalation is on after {auto_days} days "
                  "without a substantive airline response.")
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

        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        survey = db.pending_survey(self.chat_id, reply_id)
        if not survey:
            survey = db.pending_survey(self.chat_id)
        if not survey:
            self.notify("Reply to a post-flight question, or use /status.")
            return
        positive = re.fullmatch(
            r"(?:good|great|fine|perfect|all good|no issues?|it was good|"
            r"ممتاز|جيد|تمام|ما فيه مشاكل)[.! ]*", text, re.I)
        if (survey.get("status") == "asked" and positive
                and not message.get("photo")):
            db.update_survey_status(survey["flight_key"], "good")
            self.notify("Glad the flight went well ✈️")
            return
        self._collect_issue(survey, message)

    @staticmethod
    def _complaint_flight_numbers(complaint: dict) -> set[str]:
        flight = complaint.get("flight_data") or {}
        values = list(flight.get("flight_numbers") or [])
        if flight.get("flight_number"):
            values.append(flight["flight_number"])
        return {re.sub(r"\s+", "", str(value)).upper()
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
        return f"your flight{prefix} from {origin} to {destination}"

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
                attachments=intake.attachments, ai_analysis=ai_analysis)
        except ValueError as exc:
            self.notify(str(exc))
            return
        missing = missing_portal_fields(payload)
        if missing:
            db.update_survey_status(flight_key, "needs_profile")
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
                ai_analysis=ai_analysis)
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

    def _maybe_scan_mailbox(self):
        minutes = int(self.settings.get("mailbox_scan_minutes", 10))
        if minutes <= 0 or not (self.config.get("imap", {}).get("user")
                                and self.config.get("imap", {}).get("password")):
            return
        if time.monotonic() - self._last_mail_scan < minutes * 60:
            return
        self._last_mail_scan = time.monotonic()
        scan_config = json.loads(json.dumps(self.config))
        scan_config["imap"]["since_days"] = min(
            int(scan_config["imap"].get("since_days", 730)), 14)
        scan_mailbox(scan_config, log=lambda *_args, **_kwargs: None)

    def check_complaint_responses(self):
        events = db.list_mail_events()
        substantive = re.compile(
            r"resolved|resolution|decision|outcome|approved|declined|denied|"
            r"refund|compensation|reimburse|closed|تعويض|استرداد|مرفوض|إغلاق|حل",
            re.I)
        for complaint in db.list_complaints():
            if (complaint.get("kind") != "airline"
                    or complaint.get("status") not in {
                        "submitted", "accepted_pending_reference"}):
                continue
            if db.event_seen(f"closed:{complaint['flight_key']}"):
                continue
            flight = complaint.get("flight_data") or {}
            info = AIRLINES.get(flight.get("airline_code"), {})
            domains = info.get("domains") or []
            reference = str(complaint.get("reference") or "").casefold()
            created = parse_flight_time(complaint.get("created_at"))
            for event in events:
                key = f"complaint-response:{complaint['id']}:{event['id']}"
                if db.event_seen(key):
                    continue
                event_date = parse_flight_time(event.get("date"))
                if created and event_date and event_date < created:
                    continue
                sender = parseaddr(event.get("sender") or "")[1].split("@")[-1].lower()
                if domains and not any(sender == domain or sender.endswith("." + domain)
                                       for domain in domains):
                    continue
                blob = " ".join((event.get("subject") or "", event.get("body") or ""))
                if not reference:
                    capture_key = f"reference-captured:{event['id']}"
                    if db.event_seen(capture_key):
                        continue
                    captured_reference = _airline_confirmation_reference(
                        event.get("subject") or "", event.get("body") or "")
                    if captured_reference:
                        db.finish_complaint(
                            complaint["id"], "submitted", captured_reference)
                        db.mark_event_seen(capture_key)
                        complaint["reference"] = captured_reference
                        reference = captured_reference.casefold()
                        self.notify(
                            "Captured the airline complaint reference from its "
                            f"confirmation email: {captured_reference}.")
                if reference and reference not in blob.casefold():
                    continue
                analysis = None
                if (reference and self.ai.enabled
                        and self.ai.settings.get("analyze_responses", True)):
                    analysis = self.ai.analyze_response(
                        event.get("subject") or "", event.get("body") or "",
                        complaint.get("reference") or "",
                        info.get("name") or flight.get("airline_name") or "Airline")
                if analysis is not None:
                    if not analysis.get("substantive"):
                        continue
                elif not substantive.search(blob):
                    continue
                db.mark_event_seen(key)
                db.mark_event_seen(f"airline-responded:{complaint['id']}")
                if analysis:
                    amounts = "; ".join(analysis.get("amounts_or_deadlines") or [])
                    amount_line = f"\nAmounts/deadlines: {amounts}" if amounts else ""
                    response_text = (
                        f"{analysis.get('summary') or 'A substantive response was received.'}"
                        f"\nOutcome: {str(analysis.get('outcome') or 'unknown').replace('_', ' ')}"
                        f"{amount_line}\n{self.ai.name} recommends: "
                        f"{str(analysis.get('recommendation') or 'review').replace('_', ' ')}"
                        f" â€” {analysis.get('rationale') or 'Review the airline response.'}")
                else:
                    response_text = _clean_excerpt(
                        event.get("body") or event.get("subject") or "")
                self.notify(
                    f"{info.get('name') or 'The airline'} responded to complaint "
                    f"{complaint.get('reference') or ''}:\n\n{response_text}\n\n"
                    "Do you want me to escalate this to GACA?",
                    buttons=_buttons([[
                        ("Escalate to GACA", f"escalate:{complaint['flight_id']}"),
                        ("No, close", f"close_case:{complaint['flight_id']}"),
                    ]]))


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
