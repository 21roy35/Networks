"""Browser automation for official airline and GACA complaint forms.

On a desktop the browser is visible; on the VPS it runs headlessly. Login,
OTP, CAPTCHA, missing required fields, and legal declarations remain
user-controlled through the configured Telegram relay.
"""

from __future__ import annotations

import re
import io
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .airlines import AIRLINES, GACA
from . import db
from .config import TELEGRAM_EVIDENCE_DIR


logger = logging.getLogger(__name__)


_PROFILE_DIR = Path(__file__).resolve().parent / ".portal-profile"
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_TERMINAL = {
    "submitted", "accepted_pending_reference", "confirmation_unknown",
    "needs_attention", "error",
}
_BROWSER_LOCK = threading.Lock()
_VERIFICATION_HANDLER: Callable[[dict], object] | None = None
_AI_HANDLER: Callable[[dict], dict | None] | None = None
_CAPTCHA_SOLVER: Callable[[dict], dict | None] | None = None
_AI_MAX_ATTEMPTS = 3
_MAX_CAPTCHA_ROUNDS = 20
_MAX_CAPTCHA_SUBMIT_RETRIES = 2
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class PortalResult:
    status: str
    message: str
    reference: str = ""


def set_verification_handler(handler: Callable[[dict], object] | None):
    """Install a synchronous human-verification relay, normally Telegram."""
    global _VERIFICATION_HANDLER
    _VERIFICATION_HANDLER = handler


def set_ai_handler(handler: Callable[[dict], dict | None] | None,
                   max_attempts: int = 3):
    """Install the guarded AI portal-state interpreter."""
    global _AI_HANDLER, _AI_MAX_ATTEMPTS
    _AI_HANDLER = handler
    _AI_MAX_ATTEMPTS = max(1, min(int(max_attempts or 3), 10))


def set_captcha_solver(handler: Callable[[dict], dict | None] | None):
    """Install the automatic CAPTCHA solver, with Telegram kept as fallback."""
    global _CAPTCHA_SOLVER
    _CAPTCHA_SOLVER = handler


def _public_job(job: dict) -> dict:
    return {key: value for key, value in job.items()
            if key not in {"payload", "screenshot_file"}}


def portal_job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _public_job(job) if job else None


def start_portal_job(payload: dict,
                     on_complete: Callable[[PortalResult], None] | None = None,
                     on_update: Callable[[str, str, bytes | None], None]
                     | None = None) -> str:
    """Start one portal submission and return an id suitable for polling."""
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "kind": payload.get("kind") or "",
        "airline_code": payload.get("airline_code") or "",
        "flight_number": payload.get("flight_number") or "",
        "flight_key": payload.get("flight_key") or "",
        "status": "queued",
        "message": "Preparing the official complaint portal…",
        "reference": "",
        "terminal": False,
        "payload": payload,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    try:
        db.save_portal_job(job)
    except Exception:
        logger.exception("Could not persist queued portal job %s", job_id)

    def update(status: str, message: str, image: bytes | None = None):
        screenshot_file = None
        if image:
            try:
                evidence_dir = TELEGRAM_EVIDENCE_DIR / "portal_jobs"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                screenshot_file = evidence_dir / f"{job_id}.png"
                temporary = screenshot_file.with_suffix(".png.tmp")
                temporary.write_bytes(image)
                temporary.replace(screenshot_file)
            except Exception:
                logger.exception("Could not persist portal screenshot for job %s",
                                 job_id)
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(status=status, message=message,
                               terminal=status in _TERMINAL)
                if screenshot_file:
                    current.update(screenshot_available=True,
                                   screenshot_file=str(screenshot_file))
                snapshot = dict(current)
            else:
                snapshot = None
        if snapshot:
            try:
                db.save_portal_job(snapshot)
            except Exception:
                logger.exception("Could not persist portal job %s", job_id)
        logger.info("Portal job %s stage=%s terminal=%s", job_id, status,
                    status in _TERMINAL)
        if on_update:
            try:
                on_update(status, message, image)
            except Exception:
                # Telegram progress reporting must never interrupt a portal
                # submission that is otherwise proceeding normally.
                logger.exception("Portal progress callback failed for job %s",
                                 job_id)

    def worker():
        try:
            with _BROWSER_LOCK:
                result = submit_portal_claim(payload, update)
        except Exception as exc:  # pragma: no cover - final safety boundary
            logger.exception("Portal automation job %s crashed", job_id)
            result = PortalResult(
                "error", f"Portal automation stopped: {exc}")
        update(result.status, result.message)
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(reference=result.reference)
                current.pop("payload", None)
                snapshot = dict(current)
            else:
                snapshot = None
        if snapshot:
            try:
                db.save_portal_job(snapshot)
            except Exception:
                logger.exception("Could not persist final portal job %s", job_id)
        if on_complete:
            try:
                on_complete(result)
            except Exception:
                logger.exception("Portal completion callback failed for job %s",
                                 job_id)

    threading.Thread(target=worker, name=f"portal-{job_id[:8]}",
                     daemon=True).start()
    return job_id


def _official_url(payload: dict) -> str:
    if payload["kind"] == "gaca":
        return GACA["portal_url"]
    return AIRLINES.get(payload.get("airline_code"), {}).get(
        "complaint_url", "")


def _is_official_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    # Never submit real customer data to an airline's test environment even
    # when the test host is a subdomain of an otherwise trusted domain.
    if re.search(r"(^|[.-])(?:uat|preprod|staging|test)(?:[.-]|$)", host):
        return False
    allowed = {"gaca.gov.sa", "saudia.com", "flynas.com", "flyadeal.com"}
    allowed.update(domain for info in AIRLINES.values()
                   for domain in info.get("domains", []))
    return any(host == domain or host.endswith("." + domain)
               for domain in allowed)


def _launch_context(playwright):
    _PROFILE_DIR.mkdir(exist_ok=True)
    configured = os.environ.get("FLIGHTBOT_HEADLESS_BROWSER", "").strip().lower()
    headless = (configured in {"1", "true", "yes", "on"}
                if configured else sys.platform != "win32")
    options = dict(user_data_dir=str(_PROFILE_DIR), headless=headless,
                   viewport={"width": 1360, "height": 900},
                   locale="en-US", timezone_id="Asia/Riyadh",
                   user_agent=_CHROME_USER_AGENT,
                   args=["--disable-blink-features=AutomationControlled"])
    context = None
    if sys.platform == "win32":
        try:
            context = playwright.chromium.launch_persistent_context(
                channel="msedge", **options)
        except Exception:
            pass
    if context is None:
        context = playwright.chromium.launch_persistent_context(**options)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return context


def _visible(locator):
    try:
        return locator.count() and locator.first.is_visible()
    except Exception:
        return False


def _wait_for_any_visible(page, locator, timeout_ms: int = 6000):
    """Wait for any match, including later controls with duplicate labels."""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while time.monotonic() < deadline:
        try:
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            pass
        page.wait_for_timeout(150)
    return None


def _fill(page, labels: list[str], value, *, required: bool = False) -> bool:
    value = str(value or "").strip()
    if not value:
        return not required
    filled_any = False
    for label in labels:
        pattern = re.compile(label, re.I)
        for matches in (page.get_by_label(pattern),
                        page.get_by_placeholder(pattern)):
            try:
                candidates = [matches.nth(index)
                              for index in reversed(range(matches.count()))]
            except Exception:
                candidates = []
            for control in candidates:
                try:
                    if control.is_visible() and control.is_editable():
                        control.fill(value)
                        filled_any = True
                except Exception:
                    pass
    if filled_any:
        return True
    keywords = [re.sub(r"[^a-z0-9]", "", label.lower()) for label in labels]
    try:
        controls = page.locator("input:not([type=hidden]), textarea").all()
    except Exception:
        controls = []
    for control in controls:
        try:
            haystack = " ".join(filter(None, (
                control.get_attribute("name"), control.get_attribute("id"),
                control.get_attribute("placeholder"),
                control.get_attribute("aria-label")))).lower()
            compact = re.sub(r"[^a-z0-9]", "", haystack)
            if any(keyword and keyword in compact for keyword in keywords):
                if control.is_visible() and control.is_editable():
                    control.fill(value)
                    return True
        except Exception:
            continue
    return False


def _select(page, labels: list[str], choices: list[str],
            queries: list[str] | None = None) -> bool:
    choices = [choice for choice in choices if choice]
    if not choices:
        return False
    for label in labels:
        locator = page.get_by_label(re.compile(label, re.I))
        if not _visible(locator):
            continue
        for choice in choices:
            try:
                locator.first.select_option(label=re.compile(choice, re.I))
                return True
            except Exception:
                pass
    try:
        selects = page.locator("select").all()
    except Exception:
        selects = []
    for select in selects:
        if not select.is_visible():
            continue
        for choice in choices:
            try:
                options = select.locator("option").all_text_contents()
                match = next((option for option in options
                              if re.search(choice, option, re.I)), None)
                if match:
                    select.select_option(label=match.strip())
                    return True
            except Exception:
                continue
    # Saudia's current Angular form uses Material mat-select controls rather
    # than native <select> elements.
    try:
        material_fields = page.locator("mat-form-field").all()
    except Exception:
        material_fields = []
    for field in material_fields:
        try:
            if not field.is_visible():
                continue
            label_control = field.locator("mat-label")
            label_text = (label_control.first.inner_text()
                          if label_control.count() else field.inner_text())
            label_text = re.sub(r"\s+", " ", label_text).strip()
            if not any(re.search(label, label_text, re.I) for label in labels):
                continue
            control = field.locator("mat-select")
            autocomplete = field.locator("input:not([type=hidden])")
            if _visible(control):
                attempts = [(control.first, "")]
            elif _visible(autocomplete):
                attempts = [(autocomplete.first, query)
                            for query in ([""] + list(queries or []))]
            else:
                continue
            for target, query in attempts:
                # Saudia may leave a transparent Medallia feedback overlay in
                # a persistent profile. Force the intended Material control.
                target.click(force=True)
                if query:
                    try:
                        if target.is_editable():
                            target.fill(query)
                    except Exception:
                        pass
                page.wait_for_timeout(450)
                options = page.locator("mat-option, [role='option']")
                for index in range(options.count()):
                    option = options.nth(index)
                    if not option.is_visible():
                        continue
                    option_text = re.sub(
                        r"\s+", " ", option.inner_text()).strip()
                    if any(re.search(choice, option_text, re.I)
                           for choice in choices):
                        option.click(force=True)
                        page.wait_for_timeout(750)
                        return True
                page.keyboard.press("Escape")
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return False


def _material_selection_state(page, labels: list[str]) -> bool | None:
    """Return whether a matching Material field has a displayed value."""
    try:
        fields = page.locator("mat-form-field").all()
    except Exception:
        return None
    for field in fields:
        try:
            if not field.is_visible():
                continue
            label = field.locator("mat-label")
            label_text = (label.first.inner_text() if label.count()
                          else field.get_attribute("aria-label") or "")
            if not any(re.search(pattern, label_text, re.I)
                       for pattern in labels):
                continue
            select = field.locator("mat-select")
            if _visible(select):
                value = select.first.locator(
                    ".mat-mdc-select-value-text, .mat-select-value-text, "
                    ".mat-mdc-select-min-line").first
                if value.count():
                    return bool(re.sub(r"\s+", " ", value.inner_text()).strip())
                return bool(re.sub(
                    r"\s+", " ", select.first.inner_text()).strip())
            autocomplete = field.locator("input:not([type=hidden])")
            if _visible(autocomplete):
                return bool(str(autocomplete.first.input_value() or "").strip())
            return None
        except Exception:
            continue
    return None


def _ensure_saudia_selection(page, field_name: str, labels: list[str],
                              choices: list[str], queries: list[str],
                              update) -> None:
    """Set and verify a required Saudia profile dropdown without guessing."""
    for attempt in range(3):
        state = _material_selection_state(page, labels)
        if state is True:
            return
        selected = _select(page, labels, choices, queries=queries)
        state = _material_selection_state(page, labels)
        if selected and state is not False:
            return
        if attempt < 2:
            update(
                "filling",
                f"Saudia has not retained {field_name} yet. Retrying that saved value…")
            _dismiss_feedback_overlay(page)
            page.wait_for_timeout(650)
    raise RuntimeError(
        f"Saudia's production form did not retain the saved {field_name}; "
        "nothing was submitted.")


def _choose_yes(page, labels: list[str]) -> bool:
    for label in labels:
        group = page.get_by_label(re.compile(label, re.I))
        if _visible(group):
            try:
                group.first.check()
                return True
            except Exception:
                pass
    for text in ("Yes", "I am the passenger", "نعم"):
        option = page.get_by_label(re.compile(text, re.I))
        if _visible(option):
            try:
                option.first.check()
                return True
            except Exception:
                pass
    return False


def _click(page, names: list[str]) -> bool:
    for name in names:
        pattern = re.compile(name, re.I)
        for locator in (page.get_by_role("button", name=pattern),
                        page.get_by_role("link", name=pattern),
                        page.get_by_text(pattern, exact=True)):
            if _visible(locator):
                try:
                    locator.first.click()
                    return True
                except Exception:
                    pass
    return False


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _request_blocked(page) -> bool:
    """Detect WAF/error pages before they are mistaken for a form."""
    try:
        title = page.title()
    except Exception:
        title = ""
    text = _body_text(page)[:2500]
    return bool(re.search(
        r"the request is blocked|pardon our interruption|access denied|"
        r"request (?:was )?rejected|service unavailable",
        f"{title}\n{text}", re.I))


def _dismiss_feedback_overlay(page) -> bool:
    """Close Saudia's unrelated Medallia survey when it covers the form."""
    for frame in getattr(page, "frames", []):
        if not re.search(r"medallia|md-form", str(getattr(frame, "url", "")), re.I):
            continue
        for locator in (
                frame.get_by_role(
                    "button", name=re.compile(r"close(?: survey)?", re.I)),
                frame.locator("[data-aut='button-x-close']")):
            if not _visible(locator):
                continue
            try:
                locator.first.click(force=True)
                page.wait_for_timeout(600)
                return True
            except Exception:
                continue
    return False


def _captcha_completed(page) -> bool:
    """True when a rendered anti-bot widget already holds a solved token.

    Solved reCAPTCHA/hCaptcha widgets keep their iframe visible (checked
    checkbox), so widget presence alone must not stall the submission as a
    pending human step."""
    for frame in getattr(page, "frames", []):
        url = str(getattr(frame, "url", ""))
        try:
            if ("recaptcha" in url and "anchor" in url
                    and frame.locator("#recaptcha-anchor")
                    .get_attribute("aria-checked") == "true"):
                return True
            if ("hcaptcha.com" in url and "checkbox" in url
                    and frame.locator("#checkbox")
                    .get_attribute("aria-checked") == "true"):
                return True
        except Exception:
            continue
    for frame in getattr(page, "frames", []):
        try:
            tokens = frame.locator(
                "textarea[name='g-recaptcha-response'], "
                "textarea[name='h-captcha-response'], "
                "input[name='g-recaptcha-response'], "
                "input[name='h-captcha-response'], "
                "input[name='cf-turnstile-response']")
            for index in range(min(tokens.count(), 8)):
                if tokens.nth(index).input_value():
                    return True
        except Exception:
            continue
    return False


def _verification_expired(page) -> bool:
    """Detect a CAPTCHA token that the portal has explicitly rejected."""
    return bool(re.search(
        r"verification\s+(?:has\s+)?expired|captcha\s+(?:has\s+)?expired|"
        r"check\s+the\s+(?:captcha\s+)?checkbox\s+again|"
        r"(?:re-?captcha|captcha).{0,60}(?:expired|timed?\s*out)",
        _body_text(page)[:5000], re.I))


def _reset_recaptcha(page) -> None:
    """Clear a consumed/expired token and refresh the rendered widget."""
    try:
        page.evaluate("""() => {
            for (const field of document.querySelectorAll(
                    "textarea[name='g-recaptcha-response'], " +
                    "input[name='g-recaptcha-response']")) {
                const proto = field instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                if (setter) setter.call(field, ""); else field.value = "";
                field.textContent = "";
                field.dispatchEvent(new Event("input", {bubbles: true}));
                field.dispatchEvent(new Event("change", {bubbles: true}));
            }
            try { window.grecaptcha?.reset(); } catch (_) {}
        }""")
    except Exception:
        logger.debug("Could not explicitly reset reCAPTCHA", exc_info=True)


def _recaptcha_challenge(page) -> dict | None:
    candidates = []
    for position, frame in enumerate(getattr(page, "frames", [])):
        raw_url = str(getattr(frame, "url", ""))
        parsed = urlparse(raw_url)
        if "recaptcha" not in raw_url:
            continue
        params = parse_qs(parsed.query)
        site_key = (params.get("k") or [""])[0].strip()
        if not site_key:
            continue
        # A persistent browser profile can retain a hidden placeholder iframe.
        # Prefer the live checkbox and only use another keyed frame as fallback.
        score = 1 if "anchor" in parsed.path else 0
        try:
            anchor = frame.locator("#recaptcha-anchor")
            if anchor.count():
                score += 2
                if anchor.first.is_visible():
                    score += 4
        except Exception:
            pass
        data_s = (params.get("s") or [""])[0].strip()
        candidates.append((score, -position, parsed, params, site_key, data_s))
    if not candidates:
        # Angular can expose its site key before Google's iframe is attached.
        try:
            widget = page.locator(
                ".g-recaptcha[data-sitekey], [data-sitekey]").first
            site_key = str(widget.get_attribute("data-sitekey") or "").strip()
            data_s = str(widget.get_attribute("data-s") or "").strip()
        except Exception:
            site_key = ""
            data_s = ""
        if not site_key:
            return None
        try:
            user_agent = str(page.evaluate("navigator.userAgent") or "")
        except Exception:
            user_agent = _CHROME_USER_AGENT
        challenge = {
            "kind": "recaptcha",
            "website_url": page.url,
            "site_key": site_key,
            "is_invisible": False,
            "is_enterprise": False,
            "user_agent": user_agent,
            "api_domain": "google.com",
        }
        if data_s:
            challenge["data_s"] = data_s
        return challenge
    _score, _position, parsed, params, site_key, data_s = max(candidates)
    try:
        user_agent = str(page.evaluate("navigator.userAgent") or "")
    except Exception:
        user_agent = _CHROME_USER_AGENT
    challenge = {
        "kind": "recaptcha",
        "website_url": page.url,
        "site_key": site_key,
        "is_invisible": (params.get("size") or [""])[0] == "invisible",
        "is_enterprise": "/enterprise/" in parsed.path,
        "user_agent": user_agent,
        "api_domain": ("recaptcha.net"
                       if (parsed.hostname or "").endswith("recaptcha.net")
                       else "google.com"),
    }
    if data_s:
        challenge["data_s"] = data_s
    return challenge


def _hcaptcha_challenge(page) -> dict | None:
    """Describe hCaptcha even when the site nests it inside component frames."""
    for frame in getattr(page, "frames", []):
        raw_url = str(getattr(frame, "url", ""))
        parsed = urlparse(raw_url)
        if "hcaptcha.com" not in (parsed.hostname or ""):
            continue
        params = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)
        site_key = ((params.get("sitekey") or fragment.get("sitekey")
                     or params.get("site_key") or fragment.get("site_key")
                     or [""])[0].strip())
        if not site_key:
            continue
        try:
            user_agent = str(page.evaluate("navigator.userAgent") or "")
        except Exception:
            user_agent = _CHROME_USER_AGENT
        rqdata = ((params.get("rqdata") or fragment.get("rqdata")
                   or [""])[0].strip())
        challenge = {
            "kind": "hcaptcha",
            "website_url": page.url,
            "site_key": site_key,
            "is_invisible": ((params.get("size") or fragment.get("size")
                              or [""])[0] == "invisible"),
            "user_agent": user_agent,
        }
        if rqdata:
            challenge["enterprise_payload"] = {"rqdata": rqdata}
        return challenge
    return None


def _pending_captcha_kind(page) -> str:
    """Return the pending widget type, including widgets in nested frames."""
    if _captcha_completed(page):
        return ""
    for frame in getattr(page, "frames", []):
        raw_url = str(getattr(frame, "url", ""))
        try:
            if ("recaptcha" in raw_url and "anchor" in raw_url
                    and frame.locator("#recaptcha-anchor").count()):
                return "recaptcha"
            if ("hcaptcha.com" in raw_url and "checkbox" in raw_url
                    and frame.locator("#checkbox").count()):
                return "hcaptcha"
        except Exception:
            continue
    try:
        if _visible(page.locator(
                "iframe[src*='recaptcha'], .g-recaptcha")):
            return "recaptcha"
        if _visible(page.locator(
                "iframe[src*='hcaptcha'], .h-captcha")):
            return "hcaptcha"
        if _visible(page.locator(
                "iframe[src*='captcha'], iframe[title*='captcha' i]")):
            return "captcha"
    except Exception:
        pass
    return ""


def _inject_recaptcha_token(page, token: str) -> bool:
    try:
        applied = page.evaluate("""token => {
            const fields = Array.from(document.querySelectorAll(
                "textarea[name='g-recaptcha-response'], " +
                "input[name='g-recaptcha-response']"));
            for (const field of fields) {
                const proto = field instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                if (setter) setter.call(field, token); else field.value = token;
                field.dispatchEvent(new Event("input", {bubbles: true}));
                field.dispatchEvent(new Event("change", {bubbles: true}));
            }

            const callbacks = new Set();
            for (const element of document.querySelectorAll("[data-callback]")) {
                const path = (element.getAttribute("data-callback") || "").split(".");
                let value = window;
                for (const part of path) value = value?.[part];
                if (typeof value === "function") callbacks.add(value);
            }
            const seen = new WeakSet();
            const visit = (value, depth = 0) => {
                if (!value || depth > 6 || !["object", "function"].includes(typeof value)) return;
                if (seen.has(value)) return;
                seen.add(value);
                for (const key of Object.keys(value)) {
                    let child;
                    try { child = value[key]; } catch (_) { continue; }
                    if (key.toLowerCase() === "callback" && typeof child === "function") {
                        callbacks.add(child);
                    } else {
                        visit(child, depth + 1);
                    }
                }
            };
            visit(window.___grecaptcha_cfg?.clients || {});
            let called = 0;
            for (const callback of callbacks) {
                try { callback(token); called += 1; } catch (_) {}
            }
            return {fields: fields.length, callbacks: called};
        }""", token)
    except Exception:
        return False
    return bool((applied or {}).get("fields") or (applied or {}).get("callbacks"))


def _inject_hcaptcha_token(page, token: str) -> bool:
    """Apply an hCaptcha token to response fields and registered callbacks."""
    applied_fields = 0
    called_callbacks = 0
    script = """token => {
        const roots = [document];
        for (let i = 0; i < roots.length; i += 1) {
            for (const element of roots[i].querySelectorAll("*")) {
                if (element.shadowRoot) roots.push(element.shadowRoot);
            }
        }
        const fields = [];
        for (const root of roots) {
            fields.push(...root.querySelectorAll(
                "textarea[name='h-captcha-response'], " +
                "textarea[name='g-recaptcha-response'], " +
                "input[name='h-captcha-response'], " +
                "input[name='g-recaptcha-response']"));
        }
        for (const field of fields) {
            const proto = field instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
            if (setter) setter.call(field, token); else field.value = token;
            field.textContent = token;
            field.dispatchEvent(new Event("input", {bubbles: true}));
            field.dispatchEvent(new Event("change", {bubbles: true}));
        }

        const callbacks = new Set();
        for (const root of roots) {
            for (const element of root.querySelectorAll("[data-callback]")) {
                const path = (element.getAttribute("data-callback") || "").split(".");
                let value = window;
                for (const part of path) value = value?.[part];
                if (typeof value === "function") callbacks.add(value);
            }
        }
        const seen = new WeakSet();
        const visit = (value, depth = 0) => {
            if (!value || depth > 6 ||
                    !["object", "function"].includes(typeof value)) return;
            if (seen.has(value)) return;
            seen.add(value);
            for (const key of Object.keys(value)) {
                let child;
                try { child = value[key]; } catch (_) { continue; }
                if (key.toLowerCase() === "callback" &&
                        typeof child === "function") callbacks.add(child);
                else visit(child, depth + 1);
            }
        };
        visit(window.___hcaptcha_cfg || {});
        visit(window.___grecaptcha_cfg?.clients || {});
        let called = 0;
        for (const callback of callbacks) {
            try { callback(token); called += 1; } catch (_) {}
        }
        return {fields: fields.length, callbacks: called};
    }"""
    for frame in getattr(page, "frames", []):
        try:
            result = frame.evaluate(script, token) or {}
            applied_fields += int(result.get("fields") or 0)
            called_callbacks += int(result.get("callbacks") or 0)
        except Exception:
            continue
    return bool(applied_fields or called_callbacks)


def _solve_recaptcha_automatically(page, update) -> bool:
    if not _CAPTCHA_SOLVER:
        return False
    challenge = None
    for attempt in range(4):
        challenge = _recaptcha_challenge(page)
        if challenge:
            break
        if attempt < 3:
            page.wait_for_timeout(500)
    if not challenge:
        logger.warning("2Captcha skipped: no current reCAPTCHA site key was found")
        return False
    update("verification", "2Captcha is solving the reCAPTCHA automatically…")
    try:
        result = _CAPTCHA_SOLVER(challenge) or {}
    except Exception:
        logger.exception("Automatic reCAPTCHA solving failed")
        return False
    token = str(result.get("token") or "").strip()
    if not token or not _inject_recaptcha_token(page, token):
        logger.warning("2Captcha returned a token that the page could not apply")
        return False
    page.wait_for_timeout(500)
    update(
        "filling",
        "2Captcha returned a token. Validating it with the official portal now…")
    return True


def _solve_hcaptcha_automatically(page, update) -> bool:
    if not _CAPTCHA_SOLVER:
        return False
    challenge = _hcaptcha_challenge(page)
    if not challenge:
        return False
    update("verification", "2Captcha is solving the hCaptcha automaticallyâ€¦")
    try:
        result = _CAPTCHA_SOLVER(challenge) or {}
    except Exception:
        logger.exception("Automatic hCaptcha solving failed")
        return False
    token = str(result.get("token") or "").strip()
    if not token or not _inject_hcaptcha_token(page, token):
        return False
    page.wait_for_timeout(1500)
    update("filling", "2Captcha verification applied. Continuing automaticallyâ€¦")
    return True


def _needs_human_step(page) -> str:
    if _pending_captcha_kind(page):
        return "Solve the CAPTCHA challenge."
    checks = (
        ("input[name*='otp' i], input[id*='otp' i], input[autocomplete='one-time-code']",
         "Enter the OTP sent by the official portal."),
        ("iframe[src*='challenges.cloudflare.com']",
         "Approve the anti-bot verification challenge."),
        ("input[type='checkbox'][required]:not(:checked)",
         "Approve the required declaration."),
        ("input[type='password']",
         "Complete the one-time portal sign-in."),
    )
    for selector, message in checks:
        if not _visible(page.locator(selector)):
            continue
        if (("captcha" in selector or "cloudflare" in selector)
                and _captcha_completed(page)):
            continue
        return message
    if re.search(r"Nafath|نفاذ|approve (?:the )?(?:login|request)",
                 _body_text(page), re.I):
        return "Approve the Nafath or sign-in request on your phone."
    return ""


def _page_screenshot(page) -> bytes:
    try:
        return page.screenshot(type="png", full_page=False)
    except Exception:
        return b""


def _ask_verification(kind: str, message: str, page, image: bytes = b"",
                      choices: list[str] | None = None):
    if not _VERIFICATION_HANDLER:
        return None
    return _VERIFICATION_HANDLER({
        "kind": kind,
        "message": message,
        "image": image or _page_screenshot(page),
        "choices": choices or [],
        "url": page.url,
    })


def _portal_elements(page) -> list[dict]:
    """Return visible control metadata without passwords or entered values."""
    try:
        controls = page.locator(
            "input:not([type=hidden]), textarea, select, mat-select, button, "
            "a[role='button'], input[type='submit']")
        count = min(controls.count(), 80)
    except Exception:
        return []
    elements = []
    for index in range(count):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            record = control.evaluate("""el => {
                const labels = el.labels ? Array.from(el.labels)
                    .map(x => x.innerText.trim()).filter(Boolean) : [];
                const options = el.tagName.toLowerCase() === 'select'
                    ? Array.from(el.options).map(x => x.text.trim()).filter(Boolean).slice(0, 15)
                    : [];
                return {
                    tag: el.tagName.toLowerCase(),
                    type: (el.type || '').toLowerCase(),
                    label: labels.join(' / '),
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    aria_label: el.getAttribute('aria-label') || '',
                    text: ['button', 'a', 'mat-select'].includes(el.tagName.toLowerCase())
                        ? (el.innerText || '').trim().slice(0, 180) : '',
                    required: !!el.required,
                    invalid: !!el.validationMessage,
                    options,
                };
            }""")
            if str(record.get("type") or "").lower() in {
                    "password", "file", "hidden"}:
                record["text"] = ""
            elements.append(record)
        except Exception:
            continue
    return elements


def _ai_payload(payload: dict) -> dict:
    safe = {}
    for key, value in (payload or {}).items():
        if key in {"attachments", "ai_analysis"}:
            continue
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            safe[key] = str(value)[:10000]
    return safe


def _ask_ai(page, payload: dict, reason: str) -> dict | None:
    if not _AI_HANDLER:
        return None
    try:
        return _AI_HANDLER({
            "reason": reason,
            "page_url": page.url,
            "page_text": _body_text(page),
            "elements": _portal_elements(page),
            "payload": _ai_payload(payload),
            "image": _page_screenshot(page),
        })
    except Exception:
        return None


def _apply_ai_decision(page, decision: dict | None, payload: dict,
                       update) -> tuple[bool, bool]:
    """Apply one code-validated safe action. Returns handled, cancelled."""
    if not isinstance(decision, dict):
        return False, False
    state = str(decision.get("state") or "").lower()
    action = str(decision.get("action") or "").lower()
    target = str(decision.get("target") or "").strip()[:180]
    value = str(decision.get("value") or "").strip()
    summary = str(decision.get("summary") or "The portal needs attention.").strip()
    prompt = str(decision.get("user_prompt") or "").strip()
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0

    security = re.compile(
        r"captcha|otp|one.?time|password|passcode|login|sign.?in|nafath|"
        r"declaration|consent|terms|payment|card|security|verification|verify",
        re.I)
    final = re.compile(
        r"submit|send|file\s*(?:the\s*)?complaint|confirm|agree|accept\s*terms|"
        r"purchase|pay|delete|إرسال|تقديم|تأكيد",
        re.I)
    guarded_state = state in {"captcha", "otp", "login", "declaration"}

    if action == "ask_user" or guarded_state:
        if not _VERIFICATION_HANDLER:
            return False, False
        response = _ask_verification(
            "ai_assistance",
            f"Ghala-200 portal review: {summary}\n{prompt or 'Review the screenshot and complete the requested step.'}",
            page, choices=["Done", "Cancel"])
        if str(response or "").strip().lower() == "cancel":
            return True, True
        update("verification", "Ghala-200 handed the protected step back to you. Continuing safelyâ€¦")
        return True, False

    if confidence < 0.65:
        return False, False
    if action in {"fill", "select"}:
        if not target or security.search(target):
            return False, False
        allowed = {str(item).strip() for item in _ai_payload(payload).values()}
        if value not in allowed:
            return False, False
        labels = [re.escape(target)]
        if action == "fill":
            changed = _fill(page, labels, value)
        elif re.search(r"nationality", target, re.I):
            choices, queries = _nationality_selection(value)
            changed = _select(page, labels, choices, queries=queries)
        else:
            changed = _select(page, labels, [re.escape(value)])
        if changed:
            update("filling", f"Ghala-200 safely completed {target} from stored trip data.")
        return changed, False

    if action == "click":
        allowed_navigation = re.compile(
            r"^(?:next|continue|retry|back|previous|complaints?(?:\s*&\s*feedback)?|"
            r"feedback|customer relations|apply now|apply for service)$",
            re.I)
        if (not target or final.search(target) or security.search(target)
                or not allowed_navigation.fullmatch(target)):
            return False, False
        changed = _click(page, [re.escape(target)])
        if changed:
            update("filling", f"Ghala-200 selected the safe navigation step: {target}.")
        return changed, False
    return False, False


def _annotate_grid(png: bytes, count: int) -> bytes:
    if not png or count not in (9, 16):
        return png
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.open(io.BytesIO(png)).convert("RGB")
        draw = ImageDraw.Draw(image)
        side = 3 if count == 9 else 4
        width, height = image.size
        font = ImageFont.load_default(size=max(18, min(width, height) // 12))
        for index in range(count):
            row, column = divmod(index, side)
            x0, y0 = column * width / side, row * height / side
            x1, y1 = (column + 1) * width / side, (row + 1) * height / side
            draw.rectangle((x0, y0, x1, y1), outline="#ff2d2d", width=4)
            draw.text((x0 + 8, y0 + 6), str(index + 1), fill="white",
                      stroke_width=3, stroke_fill="black", font=font)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return png


def _parse_cells(response, count: int) -> list[int]:
    values = re.findall(r"\d+", str(response or ""))
    return sorted({int(value) for value in values
                   if 1 <= int(value) <= count})


def _recaptcha_checked(anchor) -> bool:
    try:
        return anchor.locator(
            "#recaptcha-anchor").get_attribute("aria-checked") == "true"
    except Exception:
        return False


def _wait_for_recaptcha_refresh(page, anchor, *, minimum_ms: int = 4500,
                                  timeout_seconds: int = 15
                                  ) -> tuple[bool, bool]:
    """Return (verified, grid_ready) after a submitted CAPTCHA round."""
    page.wait_for_timeout(minimum_ms)
    deadline = time.monotonic() + timeout_seconds
    last_image = None
    while time.monotonic() < deadline:
        if _recaptcha_checked(anchor):
            return True, True
        frame = next((item for item in page.frames
                      if "recaptcha" in item.url
                      and "bframe" in item.url
                      and _visible(item.locator("#rc-imageselect-target"))),
                     None)
        if frame:
            grid = frame.locator("#rc-imageselect-target")
            cells = frame.locator("#rc-imageselect-target td")
            selected = frame.locator(
                "#rc-imageselect-target .rc-imageselect-tileselected")
            images = grid.locator("img")
            try:
                loaded = (not images.count() or images.evaluate_all(
                    "items => items.every(img => img.complete && "
                    "img.naturalWidth > 0)"))
                ready = (cells.count() in (9, 16)
                         and selected.count() == 0 and loaded)
                current_image = grid.screenshot(type="png") if ready else None
            except Exception:
                current_image = None
            if current_image and current_image == last_image:
                return False, True
            last_image = current_image
        else:
            last_image = None
        page.wait_for_timeout(750)
    return _recaptcha_checked(anchor), False


def _click_grid_cells(cells, selected: list[int]) -> bool:
    """Click the chosen tiles without letting one flaky tile kill the job.

    Challenge tiles animate while images fade in, so a strict actionability
    wait can time out even though the tile is clickable. Retry each tile with
    a forced click before reporting failure so the caller can re-prompt with
    a fresh screenshot instead of aborting the submission."""
    for cell in selected:
        tile = cells.nth(cell - 1)
        for kwargs in ({"timeout": 8000}, {"timeout": 4000, "force": True}):
            try:
                tile.click(**kwargs)
                break
            except Exception:
                continue
        else:
            return False
    return True


def _solve_recaptcha(page, update) -> bool:
    # Saudia currently renders a placeholder anchor iframe before the real
    # interactive one. Choose the frame that actually contains a visible
    # checkbox so the verification is not abandoned before Telegram receives
    # the image challenge.
    anchor = next((frame for frame in page.frames
                   if "recaptcha" in frame.url
                   and "anchor" in frame.url
                   and _visible(frame.locator("#recaptcha-anchor"))), None)
    if anchor:
        checkbox = anchor.locator("#recaptcha-anchor")
        try:
            if checkbox.get_attribute("aria-checked") == "true":
                return True
            checkbox.click()
            page.wait_for_timeout(1800)
            if checkbox.get_attribute("aria-checked") == "true":
                return True
        except Exception:
            pass
    for round_number in range(1, _MAX_CAPTCHA_ROUNDS + 1):
        frame = next((item for item in page.frames
                      if "recaptcha" in item.url and "bframe" in item.url), None)
        if not frame:
            return bool(anchor and anchor.locator(
                "#recaptcha-anchor").get_attribute("aria-checked") == "true")
        cells = frame.locator("#rc-imageselect-target td")
        count = cells.count()
        if count not in (9, 16):
            return False
        grid = frame.locator("#rc-imageselect-target")
        try:
            image = _annotate_grid(grid.screenshot(type="png"), count)
        except Exception:
            page.wait_for_timeout(1500)
            continue
        instruction = _body_text(frame)[:500]
        response = _ask_verification(
            "captcha_grid",
            f"CAPTCHA round {round_number}: {instruction}\n"
            "Reply with the matching tile numbers, for example: 1 4 7.",
            page, image=image)
        selected = _parse_cells(response, count)
        if not selected:
            return False
        if not _click_grid_cells(cells, selected):
            update(
                "verification",
                "A CAPTCHA tile stopped responding, so a fresh challenge "
                "screenshot is on its way…")
            page.wait_for_timeout(1500)
            continue
        button = frame.locator("#recaptcha-verify-button")
        try:
            if button.count():
                button.click(timeout=8000)
        except Exception:
            pass
        verified, ready = _wait_for_recaptcha_refresh(page, anchor)
        if verified:
            update("filling", "CAPTCHA verified through Telegram. Continuing…")
            return True
        if not ready:
            update(
                "verification",
                "The next CAPTCHA image did not finish loading, so no stale screenshot was sent.")
            return False
    update(
        "verification",
        f"reCAPTCHA remained active after {_MAX_CAPTCHA_ROUNDS} completed grids.")
    return False


def _solve_hcaptcha(page, update) -> bool:
    checkbox_frame = next((frame for frame in page.frames
                           if "hcaptcha.com" in frame.url
                           and "checkbox" in frame.url), None)
    if checkbox_frame:
        checkbox = checkbox_frame.locator("#checkbox")
        try:
            if checkbox.get_attribute("aria-checked") == "true":
                return True
            checkbox.click()
            page.wait_for_timeout(1600)
        except Exception:
            pass
    for _round in range(5):
        frame = next((item for item in page.frames
                      if "hcaptcha.com" in item.url and "challenge" in item.url), None)
        if not frame:
            return bool(checkbox_frame and checkbox_frame.locator(
                "#checkbox").get_attribute("aria-checked") == "true")
        cells = frame.locator(".task-grid .task-image")
        count = cells.count()
        if count not in (9, 16):
            return False
        grid = frame.locator(".task-grid")
        try:
            image = _annotate_grid(grid.screenshot(type="png"), count)
        except Exception:
            page.wait_for_timeout(1500)
            continue
        prompt = _clean_frame_text(frame)[:500]
        response = _ask_verification(
            "captcha_grid",
            f"CAPTCHA: {prompt}\nReply with the matching tile numbers.",
            page, image=image)
        selected = _parse_cells(response, count)
        if not selected:
            return False
        if not _click_grid_cells(cells, selected):
            update(
                "verification",
                "A CAPTCHA tile stopped responding, so a fresh challenge "
                "screenshot is on its way…")
            page.wait_for_timeout(1500)
            continue
        button = frame.locator(".button-submit")
        try:
            if button.count():
                button.click(timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        if checkbox_frame and checkbox_frame.locator(
                "#checkbox").get_attribute("aria-checked") == "true":
            update("filling", "CAPTCHA verified through Telegram. Continuing…")
            return True
    return False


def _clean_frame_text(frame) -> str:
    try:
        return re.sub(r"\s+", " ", frame.locator("body").inner_text()).strip()
    except Exception:
        return "Select every matching image."


def _solve_text_captcha(page, update) -> bool:
    image = page.locator(
        "img[src*='captcha' i], img[id*='captcha' i], img[alt*='captcha' i]")
    field = page.locator(
        "input[name*='captcha' i], input[id*='captcha' i]")
    if not (_visible(image) and _visible(field)):
        return False
    response = _ask_verification(
        "captcha_text", "Reply with the characters shown in this CAPTCHA.",
        page, image=image.first.screenshot(type="png"))
    if not response:
        return False
    field.first.fill(str(response).strip())
    _click(page, ["Verify", "Continue", "Submit", "تحقق", "متابعة"])
    page.wait_for_timeout(1200)
    update("filling", "CAPTCHA answer entered. Continuing…")
    return True


def _solve_turnstile(page, update) -> bool:
    frame = next((item for item in page.frames
                  if "challenges.cloudflare.com" in item.url), None)
    if not frame:
        return False
    response = _ask_verification(
        "approval",
        "Cloudflare requires a human verification approval. Tap Approve and I’ll activate the checkbox.",
        page, choices=["Approve", "Cancel"])
    if str(response or "").lower() not in {"approve", "approved", "yes"}:
        return False
    for selector in ("input[type='checkbox']", ".ctp-checkbox-label", "label"):
        control = frame.locator(selector)
        if _visible(control):
            try:
                control.first.click()
                page.wait_for_timeout(1800)
                update("filling", "Verification approved through Telegram. Continuing…")
                return True
            except Exception:
                continue
    return False


def _solve_otp(page, update) -> bool:
    field = page.locator(
        "input[name*='otp' i], input[id*='otp' i], input[autocomplete='one-time-code']")
    if not _visible(field):
        return False
    response = _ask_verification(
        "otp", "Reply with the one-time code sent by the official portal.", page)
    code = re.sub(r"\D", "", str(response or ""))
    if not code:
        return False
    field.first.fill(code)
    _click(page, ["Verify", "Continue", "Confirm", "تحقق", "متابعة", "تأكيد"])
    page.wait_for_timeout(1200)
    update("filling", "OTP entered through Telegram. Continuing…")
    return True


def _approve_declaration(page, update) -> bool:
    boxes = page.locator("input[type='checkbox'][required]:not(:checked)")
    if not _visible(boxes):
        return False
    response = _ask_verification(
        "approval", "The official form requires a declaration. Tap Approve in Telegram after reviewing the screenshot.",
        page, choices=["Approve", "Cancel"])
    if str(response or "").lower() not in {"approve", "approved", "yes"}:
        return False
    for index in range(boxes.count()):
        boxes.nth(index).check()
    update("filling", "Declaration approved through Telegram. Continuing…")
    return True


def _wait_for_human_step(page, update, timeout_seconds: int = 600,
                         defer_captcha: bool = False) -> bool:
    message = _needs_human_step(page)
    if not message:
        return True
    captcha_kind = _pending_captcha_kind(page)
    # Required-field repair can be slow enough to age a valid CAPTCHA token.
    # Defer it until those checks are finished so the token is fresh at submit.
    if captcha_kind and defer_captcha:
        return True
    # When an automatic solver is available, do not tell the user to solve it.
    # The solver's next update explains that 2Captcha is working; Telegram is
    # mentioned only if that attempt actually fails and fallback is required.
    if not (captcha_kind and _CAPTCHA_SOLVER):
        update("verification", message)
    if _VERIFICATION_HANDLER and _solve_otp(page, update):
        pass
    elif _VERIFICATION_HANDLER and _solve_text_captcha(page, update):
        pass
    elif captcha_kind == "recaptcha":
        if _CAPTCHA_SOLVER and _solve_recaptcha_automatically(page, update):
            pass
        elif _VERIFICATION_HANDLER:
            if _CAPTCHA_SOLVER:
                update(
                    "verification",
                    "2Captcha could not complete this challenge. Falling back to Telegram…")
            if not _solve_recaptcha(page, update):
                return False
        else:
            return False
    elif captcha_kind == "hcaptcha":
        if _CAPTCHA_SOLVER and _solve_hcaptcha_automatically(page, update):
            pass
        elif _VERIFICATION_HANDLER:
            if _CAPTCHA_SOLVER:
                update(
                    "verification",
                    "2Captcha could not complete this challenge. Falling back to Telegramâ€¦")
            if not _solve_hcaptcha(page, update):
                return False
        else:
            return False
    elif _VERIFICATION_HANDLER:
        if _visible(page.locator("iframe[src*='challenges.cloudflare.com']")):
            if not _solve_turnstile(page, update):
                return False
        elif _approve_declaration(page, update):
            pass
        elif _visible(page.locator("input[type='password']")):
            response = _ask_verification(
                "login",
                "Sign in once in the persistent Edge window, then reply DONE. Passwords are never requested in Telegram.",
                page, choices=["Done", "Cancel"])
            if str(response or "").lower() == "cancel":
                return False
        else:
            response = _ask_verification(
                "approval", message + " Reply DONE after completing it.",
                page, choices=["Done", "Cancel"])
            if str(response or "").lower() == "cancel":
                return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            return False
        if not _needs_human_step(page):
            update("filling", "Verification complete. Continuing automatically…")
            return True
        time.sleep(1)
    return False


def _control_label(control) -> str:
    try:
        value = control.evaluate("""el => {
            if (el.type === 'radio') {
                const legend = el.closest('fieldset')?.querySelector('legend');
                const group = el.closest('[role="radiogroup"]');
                const groupLabel = group?.getAttribute('aria-label');
                if (legend?.innerText.trim()) return legend.innerText.trim();
                if (groupLabel) return groupLabel;
            }
            const labels = el.labels ? Array.from(el.labels)
                .map(label => label.innerText.trim()).filter(Boolean) : [];
            return labels.join(' / ') || el.getAttribute('aria-label') ||
                el.getAttribute('placeholder') || el.getAttribute('name') ||
                el.getAttribute('id') || 'required field';
        }""")
        return re.sub(r"\s+", " ", str(value or "")).strip()[:160]
    except Exception:
        return "required field"


def _control_options(control) -> list[str]:
    try:
        tag = control.evaluate("el => el.tagName.toLowerCase()")
        kind = (control.get_attribute("type") or "").lower()
        if kind == "radio":
            values = control.evaluate("""el => {
                const scope = el.form || document;
                const name = el.name;
                return Array.from(scope.querySelectorAll('input[type="radio"]'))
                    .filter(item => !name || item.name === name)
                    .map(item => {
                        const label = item.labels && item.labels[0];
                        return (label?.innerText || item.value || '').trim();
                    }).filter(Boolean);
            }""")
            return list(values or [])[:12]
        if tag != "select":
            return []
        return [re.sub(r"\s+", " ", item).strip()
                for item in control.locator("option").all_text_contents()
                if item.strip()][:12]
    except Exception:
        return []


def _apply_control_answer(control, response) -> bool:
    answer = str(response or "").strip()
    if not answer:
        return False
    try:
        tag = control.evaluate("el => el.tagName.toLowerCase()")
        kind = (control.get_attribute("type") or "").lower()
        if kind == "radio":
            return bool(control.evaluate("""(el, answer) => {
                const scope = el.form || document;
                const name = el.name;
                const wanted = answer.trim().toLowerCase();
                const choices = Array.from(
                    scope.querySelectorAll('input[type="radio"]'))
                    .filter(item => !name || item.name === name);
                const match = choices.find(item => {
                    const label = item.labels && item.labels[0];
                    const text = (label?.innerText || '').trim().toLowerCase();
                    return text === wanted || item.value.toLowerCase() === wanted ||
                        text.includes(wanted);
                });
                if (!match) return false;
                match.click();
                return true;
            }""", answer))
        if tag == "select":
            options = control.locator("option").all_text_contents()
            exact = next((option for option in options
                          if option.strip().casefold() == answer.casefold()), None)
            match = exact or next((option for option in options
                                   if answer.casefold() in option.casefold()), None)
            if not match:
                return False
            control.select_option(label=match.strip())
        else:
            control.fill(answer)
        return True
    except Exception:
        return False


def _known_control_value(control, payload: dict | None) -> str:
    """Return a saved payload value for a recognizable invalid control."""
    if not payload:
        return ""
    try:
        attributes = " ".join(filter(None, (
            _control_label(control), control.get_attribute("name"),
            control.get_attribute("id"), control.get_attribute("placeholder"),
            control.get_attribute("aria-label"),
            control.get_attribute("type"),
        ))).casefold()
    except Exception:
        attributes = _control_label(control).casefold()
    mappings = (
        (r"\bemail\b|e-mail", "email"),
        (r"country.{0,20}(?:code|territory)", "country_code"),
        (r"\b(?:mobile|phone|telephone)\b", "phone"),
        (r"\b(?:first|given).{0,10}name\b", "first_name"),
        (r"\b(?:second|middle).{0,10}name\b", "middle_name"),
        (r"\b(?:last|family).{0,10}name\b|\bsurname\b", "last_name"),
        (r"alfursan|frequent.{0,10}flyer", "alfursan_id"),
        (r"passport|national.?id|iqama|identity", "national_id"),
        (r"booking.{0,15}(?:reference|number)|\bpnr\b", "pnr"),
        (r"e-?ticket|ticket.{0,10}number", "ticket_number"),
        (r"flight.{0,10}number", "flight_number"),
        (r"flight.{0,10}date|date.{0,10}flight", "flight_date"),
        (r"\bsubject\b", "subject"),
    )
    for pattern, field in mappings:
        if re.search(pattern, attributes, re.I):
            return str(payload.get(field) or "").strip()
    return ""


def _control_screenshot(page, control) -> bytes:
    try:
        control.scroll_into_view_if_needed()
        control.evaluate("el => { el.dataset.flightdeckOutline = el.style.outline; "
                         "el.style.outline = '4px solid #e11d48'; }")
        image = _page_screenshot(page)
        control.evaluate("el => { el.style.outline = "
                         "el.dataset.flightdeckOutline || ''; "
                         "delete el.dataset.flightdeckOutline; }")
        return image
    except Exception:
        return _page_screenshot(page)


def _invalid_controls(page):
    selector = (
        "input:invalid:not([type=hidden]):not([type=checkbox]):"
        "not([type=file]):not([type=password]):"
        "not([type=submit]):not([type=button]), textarea:invalid, "
        "select:invalid")
    controls = page.locator(selector)
    found = []
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            kind = (control.get_attribute("type") or "").lower()
            usable = (control.is_enabled() if kind == "radio"
                      else control.is_editable())
            if control.is_visible() and usable:
                found.append(control)
        except Exception:
            continue
    return found


def _resolve_invalid_fields(page, update,
                            payload: dict | None = None) -> tuple[bool, bool]:
    """Ask for invalid field values in Telegram. Returns changed, cancelled."""
    changed = False
    ai_used = False
    attempts: dict[str, int] = {}
    for _round in range(10):
        controls = _invalid_controls(page)
        if not controls:
            return changed, False
        control = controls[0]
        label = _control_label(control)
        key = f"{label}:{control.get_attribute('name')}:{control.get_attribute('id')}"
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] > 2:
            return changed, False
        options = _control_options(control)
        try:
            validation = control.evaluate("el => el.validationMessage") or ""
        except Exception:
            validation = ""
        option_text = ("\nAvailable choices: " + "; ".join(options)
                       if options else "")
        known_value = _known_control_value(control, payload)
        if known_value and _apply_control_answer(control, known_value):
            changed = True
            update("filling", f"Restored saved {label} automatically.")
            page.wait_for_timeout(250)
            continue
        if not _VERIFICATION_HANDLER:
            return changed, False
        if _AI_HANDLER and payload and not ai_used:
            ai_used = True
            decision = _ask_ai(
                page, payload,
                f"A required portal control is invalid: {label}. {validation}")
            handled, cancelled = _apply_ai_decision(
                page, decision, payload, update)
            if cancelled:
                return changed, True
            if handled:
                changed = True
                page.wait_for_timeout(350)
                continue
        response = _ask_verification(
            "field_input",
            f"The official portal needs: {label}. {validation}".strip()
            + option_text + "\nReply with the value, or reply CANCEL.",
            page, image=_control_screenshot(page, control))
        if str(response or "").strip().lower() == "cancel":
            return changed, True
        if not _apply_control_answer(control, response):
            update("verification", f"The value for {label} was not accepted. Asking again…")
            continue
        changed = True
        update("filling", f"Added {label} from Telegram. Continuing…")
        page.wait_for_timeout(350)
    return changed, False


def _validation_summary(page) -> str:
    messages = []
    for control in _invalid_controls(page)[:4]:
        try:
            detail = control.evaluate("el => el.validationMessage") or ""
        except Exception:
            detail = ""
        messages.append(f"{_control_label(control)}: {detail}".strip(": "))
    alerts = page.locator(
        "[role='alert'], [aria-live='assertive'], .invalid-feedback, "
        ".field-validation-error, [class*='error-message']")
    for index in range(min(alerts.count(), 4)):
        alert = alerts.nth(index)
        try:
            if alert.is_visible():
                messages.append(re.sub(r"\s+", " ", alert.inner_text()).strip())
        except Exception:
            continue
    unique = list(dict.fromkeys(message for message in messages if message))
    return "\n".join(unique)[:700]


def _submit_fallback(page) -> bool:
    control = page.locator("button[type='submit'], input[type='submit']")
    if _visible(control):
        try:
            control.first.click()
            return True
        except Exception:
            pass
    form = page.locator("form")
    if form.count():
        try:
            form.first.evaluate("form => form.requestSubmit()")
            return True
        except Exception:
            pass
    return False


def _fill_common(page, payload: dict):
    _fill(page, ["email address", "e-mail address", "email"], payload["email"])
    _fill(page, ["phone number", "mobile number", "mobile", "phone"],
          payload["phone"])
    _fill(page, ["booking reference", "booking number", "pnr"], payload["pnr"])
    _fill(page, ["ticket number", "e-ticket"], payload["ticket_number"])
    _fill(page, ["flight number"], payload["flight_number"])
    _fill(page, ["flight date", "date of flight"], payload["flight_date"])
    _fill(page, ["first name", "given name"], payload["first_name"])
    _fill(page, ["second name", "middle name"], payload["middle_name"])
    _fill(page, ["last name", "family name", "surname"], payload["last_name"])
    _fill(page, ["full name", "passenger name"], payload["passenger_name"])
    _fill(page, ["national id", "passport", "iqama", "identity"],
          payload["national_id"])
    _fill(page, ["alfursan id", "alfursan", "frequent flyer number"],
          payload.get("alfursan_id") or "")
    _fill(page, ["subject", "request subject"], payload["subject"])
    _fill(page, ["description", "complaint details", "text of the complaint",
                 "let us know", "message", "what happened"],
          payload["description"])
    _select(page, ["title"], [re.escape(payload.get("title") or "")])
    nationality = str(payload.get("nationality") or "").strip()
    nationality_choices, nationality_queries = _nationality_selection(
        nationality)
    _select(page, ["nationality"], nationality_choices,
            queries=nationality_queries)
    country_code = str(payload.get("country_code") or "").strip()
    country_choices = [re.escape(country_code)]
    country_queries = [country_code.lstrip("+")]
    if country_code.replace(" ", "") in {"966", "+966"}:
        country_choices.append(r"saudi arabia")
        country_queries.append("Saudi")
    _select(page, ["country code", "country or territory code"],
            country_choices, queries=country_queries)
    _choose_yes(page, ["are you one of the passengers", "passenger"])
    attachments = [str(path) for path in payload.get("attachments") or []
                   if Path(path).is_file()]
    if attachments:
        inputs = page.locator("input[type='file']")
        for index in range(inputs.count()):
            control = inputs.nth(index)
            try:
                selected = attachments if control.get_attribute("multiple") is not None else attachments[:1]
                control.set_input_files(selected)
                break
            except Exception:
                continue


def _nationality_selection(nationality: str) -> tuple[list[str], list[str]]:
    """Map stored English/Arabic Saudi labels to Saudia's English dropdown."""
    nationality = str(nationality or "").strip()
    if re.fullmatch(
            r"saudi(?: arabia| arabian)?|saudi\s+arabia|"
            r"(?:المملكة\s+العربية\s+السعودية|السعودية|سعودي|سعودية)",
            nationality, re.I):
        return [r"^saudi(?: arabia| arabian)?$"], ["Saudi"]
    return [re.escape(nationality)], [nationality]


def _saudia_complaint_category(payload: dict) -> str:
    text = str(payload.get("incident") or "").casefold()
    ai_category = str(
        (payload.get("ai_analysis") or {}).get("category") or "").casefold()
    mappings = (
        # Saudia currently exposes no baggage-specific option. Baggage damage,
        # loss, or delay belongs under Quality of services and must take
        # priority over the word "delay" in phrases such as "delayed baggage".
        (r"\b(?:bag|bags|baggage|luggage|suitcase|suitcases)\b",
         "Quality of services"),
        (r"\bcancel(?:led|ed|lation)?\b", "Flight Cancellation"),
        (r"denied boarding|bumped|overbook", "Denied Boarding"),
        (r"downgrade", "Downgrade"),
        (r"seat|recline", "Seats"),
        (r"meal|food", "Meals"),
        (r"wi.?fi|internet|voucher", "In-Flight Wi-Fi Services/Vouchers"),
        (r"flight attendant|cabin crew|pilot", "Flight attendants/Pilots"),
        (r"check.?in", "Check-in Counters"),
        (r"boarding gate|\bgate\b", "Boarding Gates"),
        (r"website|online|app", "Online Services"),
        (r"call cent", "Call Center"),
        (r"alfursan|miles", "AlFursan"),
        (r"\bflight\b.{0,40}\b(?:delay|delayed|late)\b|"
         r"\b(?:delay|delayed|late)\b.{0,40}\bflight\b|"
         r"\b(?:departure|arrival|departed|arrived)\b.{0,30}\blate\b",
         "Flight Delay"),
        (r"entertainment|screen|in.?flight system|accessibility|wheelchair|"
         r"refund|damag|lost", "Quality of services"),
    )
    deterministic = next((category for pattern, category in mappings
                          if re.search(pattern, text)), None)
    if deterministic:
        return deterministic
    ai_mappings = {
        "delay": "Flight Delay",
        "cancellation": "Flight Cancellation",
        "baggage": "Quality of services",
        "seat": "Seats",
        "entertainment": "Quality of services",
        "service": "Quality of services",
        "accessibility": "Quality of services",
        "refund": "Quality of services",
        "other": "Quality of services",
    }
    return ai_mappings.get(ai_category, "Quality of services")


def _prepare_saudia(page, payload: dict, update):
    update("filling", "Filling Saudia’s production Complaints & Feedback form…")
    # The official production complaint page is directly addressable. Keep the
    # contact-page selection flow only as a compatibility fallback if Saudia
    # redirects an older URL there.
    if "complaint-form" not in urlparse(page.url).path.casefold():
        service_selected = False
        for attempt in range(3):
            _dismiss_feedback_overlay(page)
            service_selected = _select(page, ["service type"], [
                "travel complaint or compliment", "post.travel"])
            if service_selected:
                break
            if attempt < 2:
                update(
                    "opening",
                    "Saudia's production form is still loading. Waiting before "
                    "one safe reload; nothing has been submitted.",
                    _page_screenshot(page))
                page.wait_for_timeout(5000)
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4500)
        if not service_selected:
            raise RuntimeError(
                "Saudia's production form did not expose its Service Type field "
                "after three safe loading attempts; nothing was submitted.")
        page.wait_for_timeout(900)
        if not _select(page, ["travel complaint or compliment", "request type"], [
                r"^complaint$"]):
            raise RuntimeError(
                "Saudia's production form did not expose the Complaint option; "
                "nothing was submitted.")
    _wait_for_any_visible(
        page, page.get_by_label(re.compile("booking reference", re.I)), 6000)
    _fill(page, ["booking reference"], payload["pnr"])
    _fill(page, ["ticket number"], payload["ticket_number"])
    _fill(page, ["last name"], payload["last_name"])
    if _click(page, ["Next"]):
        _wait_for_any_visible(
            page, page.get_by_label(re.compile("first name", re.I)), 7000)
    lookup_text = _body_text(page)
    if re.search(r"unable to verify|trip (?:was )?not found|could not (?:find|verify)",
                 lookup_text, re.I):
        update(
            "filling",
            "Saudia could not retrieve this completed trip automatically. "
            "Continuing with the same saved booking, ticket, and passenger "
            "details on the production form.",
            _page_screenshot(page))
    _fill_common(page, payload)
    nationality = str(payload.get("nationality") or "").strip()
    nationality_choices, nationality_queries = _nationality_selection(
        nationality)
    _ensure_saudia_selection(
        page, "nationality", ["nationality"], nationality_choices,
        nationality_queries, update)

    country_code = str(payload.get("country_code") or "").strip()
    country_choices = [re.escape(country_code)]
    country_queries = [country_code.lstrip("+")]
    if country_code.replace(" ", "") in {"966", "+966"}:
        country_choices.append(r"saudi arabia")
        country_queries.append("Saudi")
    _ensure_saudia_selection(
        page, "phone country code",
        ["country code", "country or territory code"], country_choices,
        country_queries, update)
    category = _saudia_complaint_category(payload)
    if not _select(page, [r"^complaint\s*\*?$"], [
            rf"^{re.escape(category)}$"]):
        raise RuntimeError(
            f"Saudia's production form did not accept the '{category}' "
            "complaint category; nothing was submitted.")
    details = page.locator("textarea[name='descriptionInfo']")
    details_control = _wait_for_any_visible(page, details, 7000)
    if details_control is not None:
        details_control.fill(payload["description"])
    elif not _fill(page, ["describe your issue", "let us know",
                          "complaint details", "what happened", "description",
                          "message"], payload["description"]):
        raise RuntimeError(
            "Saudia's production form did not expose the complaint-details "
            "field; nothing was submitted.")
    submit = _wait_for_any_visible(
        page, page.locator("button:visible").filter(has_text=re.compile(
            r"^\s*submit\s*$", re.I)), 7000)
    if submit is None:
        raise RuntimeError(
            "Saudia's production form did not expose its final Submit button; "
            "nothing was submitted.")


def _prepare_flynas(page, payload: dict, update):
    update("filling", "Opening flynas Complaints & Feedback…")
    _click(page, ["Complaints & Feedback", "Complaint", "Feedback"])
    page.wait_for_timeout(1800)
    _fill_common(page, payload)


def _prepare_flyadeal(page, payload: dict, update):
    update("filling", "Filling flyadeal’s official request form…")
    category = ("Compensation Form" if payload.get("claim_likely")
                else "I want to provide feedback")
    _select(page, ["How can we help you today"], [re.escape(category)])
    page.wait_for_timeout(1200)
    _fill_common(page, payload)


def _gaca_categories(payload: dict) -> tuple[str, str, str]:
    """Map common incidents onto GACA's current three-level taxonomy."""
    text = str(payload.get("incident") or "").casefold()
    mappings = (
        (r"screen|entertainment|in.?flight entertainment",
         ("On Board Services", "Entertainment Services", "In- flight Screens")),
        (r"wi.?fi|internet",
         ("On Board Services", "Entertainment Services", "Internet")),
        (r"seat|recline",
         ("On Board Services", "Seats", "")),
        (r"meal|food",
         ("On Board Services", "Meals", "")),
        (r"cabin crew|flight attendant|crew behavio",
         ("On Board Services", "Crew Behavior", "")),
        (r"check.?in",
         ("Check-in Process", "", "")),
        (r"boarding|\bgate\b",
         ("Boarding Services", "", "")),
        (r"bag|baggage|luggage|suitcase",
         ("Baggage Services", "", "")),
        (r"delay|cancel|flight",
         ("Flights", "", "")),
    )
    return next((categories for pattern, categories in mappings
                 if re.search(pattern, text)),
                ("Customer Service", "", ""))


def _gaca_airline_label(payload: dict) -> str:
    if payload.get("airline_code") == "SV":
        return "Saudi Arabian Airlines"
    return str(payload.get("airline_name") or "").strip()


def _gaca_mobile(payload: dict) -> str:
    phone = re.sub(r"\D", "", str(payload.get("phone") or ""))
    code = re.sub(r"\D", "", str(payload.get("country_code") or ""))
    if code and phone.startswith(code):
        phone = phone[len(code):]
    return phone.lstrip("0") or phone


def _selectize_by_label(page, label_pattern: str, query: str,
                        choices: list[str]) -> bool:
    """Choose an item from GACA's Selectize-backed hidden selects."""
    labels = page.locator("label").all()
    for label in labels:
        try:
            if not label.is_visible() or not re.search(
                    label_pattern, label.inner_text(), re.I):
                continue
            control_id = str(label.get_attribute("for") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", control_id):
                continue
            select = page.locator(f"#{control_id}")
            if select.count() != 1:
                continue
            # Some deployments leave the native select visible.
            if select.is_visible():
                options = select.locator("option").all_text_contents()
                match = next((item for item in options if any(
                    re.search(choice, item, re.I) for choice in choices)), None)
                if match:
                    select.select_option(label=match.strip())
                    return True
            input_control = page.locator(
                f"#{control_id} + .selectize-control input")
            candidate = _wait_for_any_visible(page, input_control, 1500)
            if candidate is None:
                continue
            candidate.fill("")
            candidate.type(query)
            page.wait_for_timeout(650)
            options = page.locator(
                f"#{control_id} + .selectize-control "
                ".selectize-dropdown .option")
            for index in range(options.count()):
                option = options.nth(index)
                if not option.is_visible():
                    continue
                text = re.sub(r"\s+", " ", option.inner_text()).strip()
                if any(re.search(choice, text, re.I) for choice in choices):
                    option.click(force=True)
                    page.wait_for_timeout(350)
                    return True
        except Exception:
            continue
    return False


_GACA_CITY_NAMES = {
    "AHB": "Abha", "BAH": "Bahrain", "CAI": "Cairo",
    "DMM": "Dammam", "DXB": "Dubai", "JED": "Jeddah",
    "LHR": "London", "MED": "Madinah", "NUM": "Neom",
    "RUH": "Riyadh",
}


def _prepare_gaca(page, payload: dict, update):
    update("opening", "Opening GACA’s official Airline Complaint service…")
    if _click(page, [r"^Apply Now$", "Apply For Service", "تقديم الآن"]):
        _wait_for_any_visible(
            page, page.get_by_role("button", name=re.compile(r"^Next$", re.I)),
            7000)
    if _visible(page.locator("input[type='password']")):
        if not _wait_for_human_step(page, update):
            return
        page.wait_for_timeout(1500)
    update("filling", "GACA step 1 of 4: reviewing the escalation requirements…")
    if _click(page, [r"^Next$"]):
        _wait_for_any_visible(
            page, page.get_by_label(re.compile("first name", re.I)), 7000)

    update("filling", "GACA step 2 of 4: filling personal information…")
    _fill(page, ["first name"], payload["first_name"])
    _fill(page, ["middle name"], payload["middle_name"])
    _fill(page, ["family name", "last name"], payload["last_name"])
    _fill(page, [r"^email"], payload["email"])
    _fill(page, [r"^mobile"], _gaca_mobile(payload))
    _fill(page, ["national id", "passport number"], payload["national_id"])
    _select(page, ["gender"], [r"^Male$"])
    country_code = str(payload.get("country_code") or "").strip()
    country_choices = [re.escape(country_code)] if country_code else []
    if re.sub(r"\D", "", country_code) == "966":
        country_choices += [r"Saudi Arabia.*\+966", r"\+966"]
    _selectize_by_label(
        page, r"country\s*code", country_code or "Saudi",
        country_choices or [r"Saudi Arabia"])
    if _click(page, [r"^Next$"]):
        _wait_for_any_visible(
            page, page.get_by_label(re.compile("main category", re.I)), 7000)

    update("filling", "GACA step 3 of 4: selecting the complaint category…")
    main, sub, detail = _gaca_categories(payload)
    _select(page, [r"^main category"], [rf"^{re.escape(main)}$"])
    page.wait_for_timeout(450)
    if sub:
        _select(page, [r"^subcategory"], [rf"^{re.escape(sub)}$"])
        page.wait_for_timeout(450)
    if detail:
        _select(page, [r"sub-subcategory"], [rf"^{re.escape(detail)}$"])
    if _click(page, [r"^Next$"]):
        _wait_for_any_visible(
            page, page.get_by_label(re.compile("flight date", re.I)), 7000)

    update("filling", "GACA step 4 of 4: filling flight and complaint details…")
    origin = str(payload.get("origin") or "").strip().upper()
    destination = str(payload.get("destination") or "").strip().upper()
    if origin:
        _selectize_by_label(
            page, r"flight\s*from", origin,
            [rf"\b{re.escape(origin)}\b",
             re.escape(_GACA_CITY_NAMES.get(origin, origin))])
    if destination:
        _selectize_by_label(
            page, r"flight\s*to", destination,
            [rf"\b{re.escape(destination)}\b",
             re.escape(_GACA_CITY_NAMES.get(destination, destination))])
    airline = _gaca_airline_label(payload)
    _select(page, [r"^airline"], [rf"^{re.escape(airline)}$"])
    _fill(page, ["flight date"], payload["flight_date"])
    _fill(page, ["flight number"], payload["flight_number"])
    _fill(page, ["flight ticket number", "ticket number"],
          payload["ticket_number"])
    _fill(page, ["booking number reference", "booking reference"],
          payload["pnr"])
    _fill(page, ["airline complaint number",
                 "complaint number with the air carrier"],
          payload["airline_reference"])
    _fill(page, ["complaint date at the airline",
                 "date of complaint with the air carrier"],
          payload["airline_complaint_date"])
    _fill(page, ["complaint details", "text of the complaint"],
          payload["description"])
    attachments = [str(path) for path in payload.get("attachments") or []
                   if Path(path).is_file()]
    if attachments:
        inputs = page.locator("input[type='file']")
        if inputs.count():
            inputs.first.set_input_files(attachments)
    _wait_for_any_visible(
        page, page.get_by_role("button", name=re.compile(r"^Submit$", re.I)),
        7000)


def _prepare_generic(page, payload: dict, update):
    update("filling", "Filling the airline’s official complaint form…")
    _click(page, ["Complaint", "Complaints & Feedback", "Customer Relations",
                  "Submit a request", "Contact us"])
    page.wait_for_timeout(1200)
    _fill_common(page, payload)


def _extract_reference(text: str) -> str:
    patterns = (
        r"(?:(?:complaint|request|case)\s+)?(?:reference|case|complaint|request)\s*(?:number|no\.?|id|#)?\s*(?:is\s*)?[:#-]?\s*([A-Z]{2,10}-?\d{4,})",
        r"(?:المرجع|رقم\s*(?:الطلب|الشكوى))\s*[:#-]?\s*([A-Z0-9-]{6,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    return ""
def _extract_reference_from_url(url: str) -> str:
    match = re.search(
        r"/(?:requests?|cases?|complaints?)/([A-Z0-9-]{5,})(?:[/?#]|$)",
        url, re.I)
    return match.group(1) if match else ""


def _submission_reference(body) -> str:
    """Extract a public case/reference number from a portal JSON response."""
    if body in (None, "", False):
        return ""
    try:
        serialized = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(body)
    reference = _extract_reference(serialized)
    if reference:
        return reference
    if isinstance(body, dict):
        for key, value in body.items():
            if (re.search(r"(?:reference|complaint|request|case).*(?:number|id)|"
                          r"^(?:reference|case|complaint|request)$", str(key), re.I)
                    and isinstance(value, (str, int))):
                candidate = str(value).strip()
                # JSON response objects often expose internal request/database
                # ids under names such as requestId.  A bare integer is not
                # proof of a public case reference (Saudia's customer-facing
                # references, for example, include a C_ prefix).  Numeric-only
                # references may still be recovered from a clearly contextual
                # confirmation URL, email, SMS, or page message.
                if (re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{4,}", candidate, re.I)
                        and re.search(r"[A-Z]", candidate, re.I)
                        and re.search(r"\d", candidate)):
                    return candidate
            reference = _submission_reference(value)
            if reference:
                return reference
    elif isinstance(body, list):
        for value in body:
            reference = _submission_reference(value)
            if reference:
                return reference
    return ""


def _submission_error_detail(capture: dict | None) -> str:
    """Extract portal-provided validation messages without echoing form data."""
    if not capture:
        return ""
    body = capture.get("json")
    messages = []

    def visit(value, key: str = "", depth: int = 0):
        if depth > 5 or len(messages) >= 6:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key), depth + 1)
        elif isinstance(value, list):
            for child in value[:10]:
                visit(child, key, depth + 1)
        elif (isinstance(value, (str, int, float))
              and re.search(r"error|message|reason|detail|validation", key, re.I)):
            text = re.sub(r"\s+", " ", str(value)).strip()
            if 2 < len(text) <= 300:
                messages.append(text)

    visit(body)
    if not messages and capture.get("text"):
        text = re.sub(r"\s+", " ", str(capture["text"])).strip()
        if text and not text.startswith("<"):
            messages.append(text[:300])
    return "; ".join(dict.fromkeys(messages))[:600]


def _saudia_submission_result(capture: dict | None) -> PortalResult | None:
    """Interpret Saudia's production submission response conservatively."""
    if not capture or not capture.get("seen"):
        return None
    try:
        status = int(capture.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    body = capture.get("json")
    data = body.get("data") if isinstance(body, dict) else None
    if not 200 <= status < 300 or not data:
        detail = _submission_error_detail(capture)
        logger.warning(
            "Saudia submission rejected status=%s detail=%s",
            status, detail or "not provided")
        if re.search(
                r"(?:captcha|verification).{0,80}"
                r"(?:expired|invalid|required|failed|missing)", detail, re.I):
            return PortalResult(
                "verification_expired",
                "Saudia rejected the current verification token. A fresh token "
                "and safe resubmission are required.")
        suffix = f" Portal response: {detail}" if detail else ""
        return PortalResult(
            "error",
            "Saudia's production submission service did not accept the "
            "complaint. It is recorded as failed and may be retried safely."
            + suffix)
    reference = _submission_reference(body)
    if reference:
        return PortalResult(
            "submitted",
            "Saudia's production service accepted the complaint and returned "
            "an airline reference.",
            reference)
    return PortalResult(
        "accepted_pending_reference",
        "Saudia's production service accepted the complaint, but the required "
        "airline reference has not arrived yet. FlightDeck will monitor email "
        "and will not submit a duplicate.")


def _capture_saudia_response(response, capture: dict) -> None:
    """Capture the one production API call that proves Saudia acceptance."""
    try:
        if ("/utility/sxa-migration/form-data" not in response.url
                or str(response.request.method).upper() != "POST"):
            return
        capture.update(seen=True, status=response.status, url=response.url)
        try:
            capture["json"] = response.json()
        except Exception:
            capture["text"] = response.text()[:2000]
    except Exception:
        return




def _await_confirmation(page, before_url: str, update,
                        before_text: str = "",
                        payload: dict | None = None,
                        timeout_seconds: int = 120,
                        submission_capture: dict | None = None,
                        ignore_initial_expiry: bool = False) -> PortalResult:
    started = time.monotonic()
    deadline = started + timeout_seconds
    waiting_reported = False
    while time.monotonic() < deadline:
        is_saudia = bool(payload and payload.get("airline_code") == "SV")
        if (is_saudia and _verification_expired(page)
                and not (ignore_initial_expiry
                         and time.monotonic() - started < 2)):
            return PortalResult(
                "verification_expired",
                "Saudia expired the verification token before accepting the "
                "complaint. A fresh verification and submit attempt are required.")
        saudia_result = _saudia_submission_result(submission_capture)
        if saudia_result:
            if saudia_result.status == "verification_expired":
                return saudia_result
            # Give Angular a brief opportunity to render the specific validation
            # error before classifying a generic rejected API response.
            if not (saudia_result.status == "error"
                    and time.monotonic() - started < 2):
                if is_saudia and saudia_result.status == "error":
                    return saudia_result
                update(
                    saudia_result.status,
                    saudia_result.message,
                    _page_screenshot(page) if not page.is_closed() else None)
                return saudia_result
        if page.is_closed():
            if payload and payload.get("airline_code") == "SV":
                return PortalResult(
                    "error",
                    "Saudia's production form closed without a verified "
                    "acceptance response or airline reference. The attempt is "
                    "recorded as failed.")
            return PortalResult(
                "confirmation_unknown",
                "The form was sent once, but the portal closed before a "
                "confirmation could be read.")
        if _request_blocked(page):
            if payload and payload.get("airline_code") == "SV":
                return PortalResult(
                    "error",
                    "Saudia blocked the confirmation page before its production "
                    "service verified acceptance. The attempt is recorded as "
                    "failed, not submitted.")
            return PortalResult(
                "confirmation_unknown",
                "The form was sent once, but the official site blocked the "
                "confirmation page. FlightDeck will not submit it again.")
        text = _body_text(page)
        reference = _extract_reference(text) or _extract_reference_from_url(page.url)
        success = re.search(
            r"thank you|successfully submitted|request (?:was )?received|"
            r"complaint (?:was )?received|تم (?:استلام|إرسال)|رقم (?:الطلب|الشكوى)",
            text, re.I)
        new_reference = reference and reference not in before_text
        if new_reference or success or re.search(r"success|thank", page.url, re.I):
            if (payload and payload.get("airline_code") == "SV"
                    and not reference):
                result = PortalResult(
                    "accepted_pending_reference",
                    "Saudia displayed a readable acceptance confirmation, but "
                    "has not returned the required airline reference yet. "
                    "FlightDeck will monitor email and will not submit a duplicate.")
                update(result.status, result.message, _page_screenshot(page))
                return result
            update(
                "submitted",
                "The official website confirmed the complaint submission.",
                _page_screenshot(page))
            return PortalResult(
                "submitted", "Submitted through the official website.", reference)
        human = _needs_human_step(page)
        if human:
            if (is_saudia and _pending_captcha_kind(page)
                    and time.monotonic() - started >= 2):
                return PortalResult(
                    "verification_expired",
                    "Saudia requires a fresh verification before the complaint "
                    "can be submitted again.")
            update("verification", human)
            if _VERIFICATION_HANDLER and _wait_for_human_step(page, update):
                update("submitting", "Verification complete. Waiting for confirmation…")
        elif not waiting_reported and time.monotonic() - started > 12:
            waiting_reported = True
            update(
                "submitting",
                "The complaint was sent once. Waiting for the official "
                "confirmation without clicking Submit again.",
                _page_screenshot(page))
        time.sleep(1)
    if payload and payload.get("airline_code") == "SV":
        return PortalResult(
            "error",
            "Saudia returned neither a verified production acceptance nor an "
            "airline reference. The attempt is recorded as failed and may be "
            "retried safely.")
    return PortalResult(
        "confirmation_unknown",
        "The complaint was sent once, but the official portal did not expose "
        "a readable confirmation. FlightDeck will not submit it again.")


def _submit_with_captcha_recovery(page, payload: dict, update) -> PortalResult:
    """Submit once, but safely reacquire an expired CAPTCHA token when needed."""
    is_saudia = payload.get("airline_code") == "SV"
    submission_capture = {} if is_saudia else None
    if submission_capture is not None:
        page.on(
            "response",
            lambda response: _capture_saudia_response(
                response, submission_capture))
    attempts = 1 + (_MAX_CAPTCHA_SUBMIT_RETRIES if is_saudia else 0)
    for attempt in range(attempts):
        if attempt:
            _reset_recaptcha(page)
            page.wait_for_timeout(700)
        if not _wait_for_human_step(page, update):
            return PortalResult(
                "needs_attention",
                "The final CAPTCHA or verification was not completed, so the "
                "complaint was not submitted.")

        had_expired_message = _verification_expired(page) if is_saudia else False
        before_text = _body_text(page)
        before_url = page.url
        if submission_capture is not None:
            submission_capture.clear()
        update(
            "submitting",
            ("Verification is fresh. Submitting to the official website now…"
             if attempt else
             "Finished the form checks. Submitting to the official website now…"),
            _page_screenshot(page))
        clicked = _click(page, [
            "Submit", "Send", "File complaint", "Submit request",
            "إرسال", "تقديم"])
        if not clicked:
            if _VERIFICATION_HANDLER:
                response = _ask_verification(
                    "approval",
                    "The official site changed its final button. Review the "
                    "screenshot and tap Submit to continue from Telegram.",
                    page, choices=["Submit", "Cancel"])
                if str(response or "").lower() == "cancel":
                    return PortalResult(
                        "needs_attention",
                        "Portal submission was cancelled in Telegram.")
                clicked = (str(response or "").lower() == "submit"
                           and _submit_fallback(page))
            if not clicked:
                return PortalResult(
                    "needs_attention",
                    "The official site changed its final Submit control and no "
                    "complaint was sent.")

        result = _await_confirmation(
            page, before_url, update, before_text, payload,
            submission_capture=submission_capture,
            ignore_initial_expiry=had_expired_message)
        if (result.status == "error" and is_saudia
                and attempt + 1 < attempts):
            changed, cancelled = _resolve_invalid_fields(page, update, payload)
            if cancelled:
                return PortalResult(
                    "needs_attention", "Portal input was cancelled in Telegram.")
            if not changed and _AI_HANDLER:
                decision = _ask_ai(
                    page, payload,
                    "Saudia rejected the production request. Diagnose the visible "
                    "validation state and choose one safe corrective action. "
                    f"Response: {_submission_error_detail(submission_capture) or 'none'}")
                action = str((decision or {}).get("action") or "").lower()
                handled, cancelled = _apply_ai_decision(
                    page, decision, payload, update)
                if cancelled:
                    return PortalResult(
                        "needs_attention", "Portal recovery was cancelled in Telegram.")
                changed = handled and action in {"fill", "select", "click"}
            if changed:
                update(
                    "filling",
                    "The rejected form exposed a correctable field. It was restored "
                    "from saved data; refreshing verification before one safe retry…",
                    _page_screenshot(page))
                continue
        if result.status != "verification_expired":
            return result
        if attempt + 1 >= attempts:
            return PortalResult(
                "error",
                "Saudia expired verification repeatedly. The complaint was not "
                "accepted after three safe attempts and can be retried later.")
        update(
            "verification",
            f"Saudia expired verification before accepting the complaint. "
            f"Refreshing it with 2Captcha and retrying automatically "
            f"({attempt + 1}/{_MAX_CAPTCHA_SUBMIT_RETRIES})…",
            _page_screenshot(page))
    return PortalResult("error", "The portal submission did not complete.")


def submit_portal_claim(payload: dict, update: Callable[..., None]) -> PortalResult:
    """Open, fill and submit one official web form in the managed browser."""
    url = _official_url(payload)
    if not url or not _is_official_url(url):
        return PortalResult(
            "error", "No verified official complaint website is configured for this airline.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return PortalResult(
            "error", "Browser automation is not installed. Run: pip install -r requirements.txt")

    with sync_playwright() as playwright:
        context = _launch_context(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            update("opening", "Opening the official website in Microsoft Edge…")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            if (_request_blocked(page)
                    and payload.get("airline_code") == "SV"):
                for attempt in range(2):
                    update(
                        "opening",
                        "Saudia's production security page has not released the "
                        "form yet. Waiting before a safe reload; nothing has "
                        "been submitted.",
                        _page_screenshot(page))
                    page.wait_for_timeout(5000 + attempt * 2000)
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4500)
                    if not _request_blocked(page):
                        break
            if _request_blocked(page):
                return PortalResult(
                    "needs_attention",
                    "The official site blocked the VPS browser request. FlightDeck "
                    "stopped instead of treating the block page as a complaint form.")
            _click(page, ["Accept", "Accept all", "Allow all", "موافق"])
            page.wait_for_timeout(500)
            update(
                "filling",
                "The official website loaded. Starting the saved form details.",
                _page_screenshot(page))
            kind = payload["kind"]
            code = payload.get("airline_code")
            if kind == "gaca":
                _prepare_gaca(page, payload, update)
            elif code == "SV":
                _prepare_saudia(page, payload, update)
            elif code == "XY":
                _prepare_flynas(page, payload, update)
            elif code == "F3":
                _prepare_flyadeal(page, payload, update)
            else:
                _prepare_generic(page, payload, update)

            if _request_blocked(page):
                return PortalResult(
                    "needs_attention",
                    "The official site blocked the VPS browser while preparing the form.")

            update(
                "reviewing",
                "Finished filling the known details. Checking verification and required fields.",
                _page_screenshot(page))
            if not _wait_for_human_step(page, update, defer_captcha=True):
                return PortalResult(
                    "needs_attention",
                    "Login or verification was not completed before the portal timed out.")
            _changed, cancelled = _resolve_invalid_fields(page, update, payload)
            if cancelled:
                return PortalResult(
                    "needs_attention", "Portal input was cancelled in Telegram.")
            # Mount the final widget, solve it once all slower field work is done,
            # and submit while the resulting token is still fresh.
            page.wait_for_timeout(500)
            return _submit_with_captcha_recovery(page, payload, update)
        finally:
            context.close()

