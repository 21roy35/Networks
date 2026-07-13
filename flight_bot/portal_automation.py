"""Browser automation for official airline and GACA complaint forms.

On a desktop the browser is visible; on the VPS it runs headlessly. Login,
OTP, CAPTCHA, missing required fields, and legal declarations remain
user-controlled through the configured Telegram relay.
"""

from __future__ import annotations

import re
import io
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .airlines import AIRLINES, GACA


_PROFILE_DIR = Path(__file__).resolve().parent / ".portal-profile"
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_TERMINAL = {"submitted", "needs_attention", "error"}
_BROWSER_LOCK = threading.Lock()
_VERIFICATION_HANDLER: Callable[[dict], object] | None = None
_AI_HANDLER: Callable[[dict], dict | None] | None = None
_AI_MAX_ATTEMPTS = 3
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


def _public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key != "payload"}


def portal_job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _public_job(job) if job else None


def start_portal_job(payload: dict,
                     on_complete: Callable[[PortalResult], None] | None = None) -> str:
    """Start one portal submission and return an id suitable for polling."""
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "message": "Preparing the official complaint portal…",
        "reference": "",
        "terminal": False,
        "payload": payload,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def update(status: str, message: str):
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(status=status, message=message,
                               terminal=status in _TERMINAL)

    def worker():
        try:
            with _BROWSER_LOCK:
                result = submit_portal_claim(payload, update)
        except Exception as exc:  # pragma: no cover - final safety boundary
            result = PortalResult(
                "error", f"Portal automation stopped: {exc}")
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(status=result.status, message=result.message,
                               reference=result.reference,
                               terminal=result.status in _TERMINAL)
                current.pop("payload", None)
        if on_complete:
            on_complete(result)

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


def _fill(page, labels: list[str], value, *, required: bool = False) -> bool:
    value = str(value or "").strip()
    if not value:
        return not required
    for label in labels:
        pattern = re.compile(label, re.I)
        for locator in (page.get_by_label(pattern),
                        page.get_by_placeholder(pattern)):
            if _visible(locator):
                try:
                    locator.first.fill(value)
                    return True
                except Exception:
                    pass
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


def _select(page, labels: list[str], choices: list[str]) -> bool:
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
            field_text = re.sub(r"\s+", " ", field.inner_text()).strip()
            if not any(re.search(label, field_text, re.I) for label in labels):
                continue
            control = field.locator("mat-select")
            if not _visible(control):
                continue
            # Saudia may leave a transparent Medallia feedback overlay in a
            # persistent profile. The field itself is visible and enabled;
            # force the intended Material control instead of the overlay.
            control.first.click(force=True)
            page.wait_for_timeout(300)
            options = page.locator("mat-option")
            for index in range(options.count()):
                option = options.nth(index)
                if not option.is_visible():
                    continue
                option_text = re.sub(r"\s+", " ", option.inner_text()).strip()
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


def _needs_human_step(page) -> str:
    checks = (
        ("input[name*='otp' i], input[id*='otp' i], input[autocomplete='one-time-code']",
         "Enter the OTP sent by the official portal."),
        ("iframe[src*='captcha'], iframe[title*='captcha' i], .g-recaptcha, .h-captcha",
         "Solve the CAPTCHA challenge."),
        ("iframe[src*='challenges.cloudflare.com']",
         "Approve the anti-bot verification challenge."),
        ("input[type='checkbox'][required]:not(:checked)",
         "Approve the required declaration."),
        ("input[type='password']",
         "Complete the one-time portal sign-in."),
    )
    for selector, message in checks:
        if _visible(page.locator(selector)):
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
            "input:not([type=hidden]), textarea, select, button, "
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
                    text: ['button', 'a'].includes(el.tagName.toLowerCase())
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
        changed = (_fill(page, labels, value) if action == "fill"
                   else _select(page, labels, [re.escape(value)]))
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


def _solve_recaptcha(page, update) -> bool:
    anchor = next((frame for frame in page.frames
                   if "recaptcha" in frame.url and "anchor" in frame.url), None)
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
    for _round in range(5):
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
        image = _annotate_grid(grid.screenshot(type="png"), count)
        instruction = _body_text(frame)[:500]
        response = _ask_verification(
            "captcha_grid",
            f"CAPTCHA: {instruction}\nReply with the matching tile numbers, for example: 1 4 7.",
            page, image=image)
        selected = _parse_cells(response, count)
        if not selected:
            return False
        for cell in selected:
            cells.nth(cell - 1).click()
        button = frame.locator("#recaptcha-verify-button")
        if button.count():
            button.click()
        page.wait_for_timeout(2200)
        if anchor and anchor.locator(
                "#recaptcha-anchor").get_attribute("aria-checked") == "true":
            update("filling", "CAPTCHA verified through Telegram. Continuing…")
            return True
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
        image = _annotate_grid(grid.screenshot(type="png"), count)
        prompt = _clean_frame_text(frame)[:500]
        response = _ask_verification(
            "captcha_grid",
            f"CAPTCHA: {prompt}\nReply with the matching tile numbers.",
            page, image=image)
        selected = _parse_cells(response, count)
        if not selected:
            return False
        for cell in selected:
            cells.nth(cell - 1).click()
        button = frame.locator(".button-submit")
        if button.count():
            button.click()
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


def _wait_for_human_step(page, update, timeout_seconds: int = 600) -> bool:
    message = _needs_human_step(page)
    if not message:
        return True
    update("verification", message)
    if _VERIFICATION_HANDLER:
        if _solve_otp(page, update):
            pass
        elif _solve_text_captcha(page, update):
            pass
        elif _visible(page.locator("iframe[src*='recaptcha']")):
            if not _solve_recaptcha(page, update):
                return False
        elif _visible(page.locator("iframe[src*='hcaptcha']")):
            if not _solve_hcaptcha(page, update):
                return False
        elif _visible(page.locator("iframe[src*='challenges.cloudflare.com']")):
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
    if not _VERIFICATION_HANDLER:
        return False, False
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
    _fill(page, ["full name", "passenger name", "name"], payload["passenger_name"])
    _fill(page, ["national id", "passport", "iqama", "identity"],
          payload["national_id"])
    _fill(page, ["subject", "request subject"], payload["subject"])
    _fill(page, ["description", "complaint details", "text of the complaint",
                 "let us know", "message", "what happened"],
          payload["description"])
    _select(page, ["title"], [re.escape(payload.get("title") or "")])
    _select(page, ["nationality"], [re.escape(payload.get("nationality") or "")])
    _select(page, ["country code", "country or territory code"],
            [re.escape(payload.get("country_code") or "")])
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


def _prepare_saudia(page, payload: dict, update):
    update("filling", "Filling Saudia’s official Complaints & Feedback form…")
    _select(page, ["service type"], [
        "travel complaint or compliment", "post.travel"])
    page.wait_for_timeout(900)
    _select(page, ["travel complaint or compliment", "request type"], [
        r"^complaint$"])
    page.wait_for_timeout(1200)
    _fill(page, ["booking reference"], payload["pnr"])
    _fill(page, ["ticket number"], payload["ticket_number"])
    _fill(page, ["last name"], payload["last_name"])
    if _click(page, ["Next"]):
        page.wait_for_timeout(3500)
    _fill_common(page, payload)


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


def _prepare_gaca(page, payload: dict, update):
    update("opening", "Opening GACA’s official Airline Complaint service…")
    _click(page, ["Apply Now", "Apply For Service", "تقديم الآن"])
    page.wait_for_timeout(1800)
    if _visible(page.locator("input[type='password']")):
        if not _wait_for_human_step(page, update):
            return
        page.wait_for_timeout(1500)
    update("filling", "Filling the GACA airline-escalation form…")
    _fill_common(page, payload)
    _fill(page, ["itinerary", "route"], payload["route"])
    _fill(page, ["time", "event time"], payload["event_time"])
    _fill(page, ["air carrier", "airline", "carrier"], payload["airline_name"])
    _fill(page, ["complaint number with the air carrier",
                 "airline complaint number", "carrier complaint reference"],
          payload["airline_reference"])
    _fill(page, ["date of complaint with the air carrier",
                 "airline complaint date"], payload["airline_complaint_date"])


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




def _await_confirmation(page, before_url: str, update,
                        before_text: str = "",
                        payload: dict | None = None,
                        timeout_seconds: int = 600) -> PortalResult:
    started = time.monotonic()
    deadline = started + timeout_seconds
    prompted = False
    last_assistance = 0.0
    ai_attempts = 0
    while time.monotonic() < deadline:
        if page.is_closed():
            return PortalResult("needs_attention",
                                "The official portal was closed before a confirmation was detected.")
        if _request_blocked(page):
            return PortalResult(
                "needs_attention",
                "The official site blocked the VPS browser request. This is not a "
                "CAPTCHA or a missing form field, so no verification reply was applied.")
        text = _body_text(page)
        reference = _extract_reference(text) or _extract_reference_from_url(page.url)
        success = re.search(
            r"thank you|successfully submitted|request (?:was )?received|"
            r"complaint (?:was )?received|تم (?:استلام|إرسال)|رقم (?:الطلب|الشكوى)",
            text, re.I)
        new_reference = reference and reference not in before_text
        if new_reference or success or re.search(r"success|thank", page.url, re.I):
            return PortalResult(
                "submitted", "Submitted through the official website.", reference)
        human = _needs_human_step(page)
        if human:
            update("verification", human)
            prompted = True
            if _VERIFICATION_HANDLER and _wait_for_human_step(page, update):
                update("submitting", "Verification complete. Waiting for confirmation…")
        elif page.url != before_url and prompted:
            update("submitting", "Verification complete. Waiting for confirmation…")
        elif (time.monotonic() - started > 12
              and time.monotonic() - last_assistance > 30):
            last_assistance = time.monotonic()
            changed, cancelled = _resolve_invalid_fields(page, update)
            if cancelled:
                return PortalResult(
                    "needs_attention", "Portal input was cancelled in Telegram.")
            handled = False
            if changed:
                _click(page, ["Submit", "Send", "Continue", "Confirm",
                              "إرسال", "تقديم", "متابعة", "تأكيد"])
                update("submitting", "Telegram inputs applied. Trying the official portal again…")
                prompted = True
            elif (_AI_HANDLER and payload
                  and ai_attempts < _AI_MAX_ATTEMPTS):
                ai_attempts += 1
                decision = _ask_ai(
                    page, payload,
                    "The form was submitted or advanced, but no deterministic "
                    "confirmation appeared. Identify a safe correction or navigation step.")
                handled, cancelled = _apply_ai_decision(
                    page, decision, payload, update)
                if cancelled:
                    return PortalResult(
                        "needs_attention", "Portal assistance was cancelled in Telegram.")
                if handled:
                    prompted = True
                    continue
                # If AI cannot take a code-validated action, fall through to
                # the existing screenshot-based human review on this cycle.
                if not _VERIFICATION_HANDLER:
                    update("verification",
                           "Ghala-200 could not safely resolve the portal state.")
                    prompted = True
            if not changed and _VERIFICATION_HANDLER and not handled:
                details = _validation_summary(page)
                extra = f"\n\nPortal message:\n{details}" if details else ""
                response = _ask_verification(
                    "approval",
                    "The official portal has not confirmed yet. Review this screenshot, then tap Retry to submit again or Cancel to stop safely."
                    + extra,
                    page, choices=["Retry", "Cancel"])
                prompted = True
                if str(response or "").lower() == "cancel":
                    return PortalResult(
                        "needs_attention", "Portal submission was cancelled in Telegram.")
                if str(response or "").lower() == "retry":
                    if not _click(page, [
                            "Submit", "Send", "Continue", "Confirm",
                            "إرسال", "تقديم", "متابعة", "تأكيد"]):
                        _submit_fallback(page)
                    update("submitting", "Retry approved in Telegram. Waiting for confirmation…")
            elif not changed and not _VERIFICATION_HANDLER:
                update("verification",
                       "The official site needs one correction or confirmation.")
                prompted = True
        time.sleep(1)
    return PortalResult(
        "needs_attention",
        "The official portal did not expose a confirmation before the tracking window ended.")


def submit_portal_claim(payload: dict, update: Callable[[str, str], None]) -> PortalResult:
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
            if _request_blocked(page):
                return PortalResult(
                    "needs_attention",
                    "The official site blocked the VPS browser request. FlightDeck "
                    "stopped instead of treating the block page as a complaint form.")
            _click(page, ["Accept", "Accept all", "Allow all", "موافق"])
            page.wait_for_timeout(500)
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

            if not _wait_for_human_step(page, update):
                return PortalResult(
                    "needs_attention",
                    "Login or verification was not completed before the portal timed out.")
            _changed, cancelled = _resolve_invalid_fields(page, update, payload)
            if cancelled:
                return PortalResult(
                    "needs_attention", "Portal input was cancelled in Telegram.")
            update("submitting", "Submitting to the official website…")
            before_text = _body_text(page)
            before_url = page.url
            if not _click(page, ["Submit", "Send", "File complaint",
                                 "Submit request", "إرسال", "تقديم"]):
                if _VERIFICATION_HANDLER:
                    response = _ask_verification(
                        "approval",
                        "The official site changed its final button. Review the screenshot and tap Submit to continue from Telegram.",
                        page, choices=["Submit", "Cancel"])
                    if str(response or "").lower() == "cancel":
                        return PortalResult(
                            "needs_attention", "Portal submission was cancelled in Telegram.")
                    if str(response or "").lower() == "submit":
                        _submit_fallback(page)
                update("verification", "The official site changed its final control. Telegram assistance is active while confirmation is tracked.")
                return _await_confirmation(
                    page, before_url, update, before_text, payload)
            return _await_confirmation(
                page, before_url, update, before_text, payload)
        finally:
            context.close()

