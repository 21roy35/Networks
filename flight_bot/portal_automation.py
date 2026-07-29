"""Browser automation for official airline and GACA complaint forms.

On a desktop the browser is visible; on the VPS it runs headlessly. Login,
OTP, CAPTCHA, missing required fields, and legal declarations remain
user-controlled through the configured Telegram relay.
"""

from __future__ import annotations

import re
import io
import json
import hashlib
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from .airlines import AIRLINES, GACA
from . import db
from . import gaca_normal_browser
from .config import TELEGRAM_EVIDENCE_DIR


logger = logging.getLogger(__name__)


_PROFILE_DIR = Path(__file__).resolve().parent / ".portal-profile"
_GACA_PROFILE_DIR = Path(
    os.environ.get("FLIGHTBOT_GACA_PROFILE_DIR", "").strip()
    or (Path(__file__).resolve().parent.parent / ".gaca-portal-profile"))
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_TERMINAL = {
    "submitted", "success", "accepted_pending_reference",
    "confirmation_unknown", "needs_attention", "error", "quarantined",
    "superseded",
}
_BROWSER_LOCK = threading.Lock()
_VERIFICATION_HANDLER: Callable[[dict], object] | None = None
_AI_HANDLER: Callable[[dict], dict | None] | None = None
_CATEGORY_HANDLER: Callable[..., dict | None] | None = None
_CAPTCHA_SOLVER: Callable[[dict], dict | None] | None = None
_AI_MAX_ATTEMPTS = 3
_MAX_CAPTCHA_ROUNDS = 20
_MAX_CAPTCHA_SUBMIT_RETRIES = 2
@dataclass(frozen=True)
class PortalResult:
    status: str
    message: str
    reference: str = ""
    retry_safe: bool = False
    error_code: str = ""


class GacaWafBlockedError(RuntimeError):
    """GACA rejected the browser/network identity before final Submit."""

    code = "gaca_waf_blocked"


class GacaIdentityRateLimitError(RuntimeError):
    """GACA temporarily rejected one passenger identity at Step 2."""

    code = "gaca_identity_rate_limited"


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


def set_category_handler(handler: Callable[..., dict | None] | None):
    """Install Ghala's constrained category chooser for live portal options."""
    global _CATEGORY_HANDLER
    _CATEGORY_HANDLER = handler


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
        if job:
            return _public_job(job)
    try:
        stored = db.get_portal_job(job_id)
    except Exception:
        stored = None
    return _public_job(stored) if stored else None


def _finalize_persisted_complaint(payload: dict,
                                  result: PortalResult) -> None:
    """Apply a terminal result even after the original callback was lost."""
    complaint_id = payload.get("portal_complaint_id")
    if not complaint_id:
        return
    if result.status == "submitted":
        status = "submitted"
    elif result.status == "accepted_pending_reference":
        status = "accepted_pending_reference"
    elif payload.get("kind") == "gaca":
        status = "needs_attention"
    else:
        status = "failed"
    db.finish_complaint(
        int(complaint_id), status, result.reference or None,
        submitted_text=payload.get("description") or None,
        portal_category=(
            payload.get("selected_complaint_category") or None))
    flight_key = str(payload.get("flight_key") or "")
    if payload.get("kind") == "airline" and flight_key:
        db.update_survey_status(
            flight_key,
            "filed" if result.status == "submitted" else "needs_attention")
    inflight_key = str(payload.get("portal_inflight_key") or "")
    auto_key = str(payload.get("portal_auto_key") or "")
    if inflight_key:
        db.clear_event_seen(inflight_key)
    if auto_key:
        if result.status == "submitted":
            db.mark_event_seen(auto_key)
        else:
            db.clear_event_seen(auto_key)


def _retry_is_safe(result: PortalResult, last_stage: str) -> bool:
    """Only replay attempts proven not to have reached an ambiguous Submit."""
    if result.retry_safe or result.status == "verification_expired":
        return True
    if re.search(r"\bcancel(?:led|ed)?\b", result.message, re.I):
        return False
    return (
        result.status in {"error", "needs_attention"}
        and last_stage in {
            "queued", "leased", "opening", "filling", "reviewing",
            "verification",
        }
    )


def _retry_minimum_delay(result: PortalResult) -> int:
    """Avoid hammering a regulator login endpoint during a known outage."""
    message = str(result.message or "")
    if re.search(
        r"GACA[\s\S]{0,120}Nafath[\s\S]{0,120}"
        r"(?:authentication.*failed|endpoint.*reject|unavailable)",
        message,
        re.I,
    ):
        return 60 * 60
    if re.search(
            r"GACA[\s\S]{0,160}too many submission attempts",
            message, re.I):
        return 60 * 60
    return 5 * 60


def _maybe_use_gaca_email_fallback(
    job_id: str,
    payload: dict,
    result: PortalResult,
    update: Callable[[str, str, bytes | None], None],
) -> PortalResult:
    """Send an audit copy without treating email as a portal submission.

    GACA's email server accepting a message does not prove that a complaint
    was registered in the GACA portal.  The durable portal job therefore
    remains retryable until the portal itself, an official acknowledgement,
    or an explicit regulator reference confirms acceptance.
    """
    if payload.get("kind") != "gaca" or result.status not in {
        "error", "needs_attention"
    }:
        return result
    if not re.search(
        r"\bWAF\b|blocked the .*browser|page can.?t be displayed|"
        r"security (?:page|error)|incident ID|"
        r"BrowserType\.launch_persistent_context|"
        r"Target page, context or browser has been closed",
        str(result.message or ""),
        re.I,
    ):
        return result
    from .config import load_config
    from .gaca_email import deliver_gaca_email

    config = load_config()
    if not (config.get("gaca_email") or {}).get("enabled"):
        return result
    update(
        "email_copy",
        "GACA's web platform is unavailable. Sending an audit copy through "
        "GACA's official email channel while keeping the portal job open.",
    )
    delivery = deliver_gaca_email(payload, job_id, config)
    if not delivery.accepted:
        return PortalResult(
            result.status,
            f"{result.message} The official email fallback also failed: "
            f"{delivery.error}",
            retry_safe=True,
            error_code=result.error_code,
        )
    payload["gaca_submission_channel"] = "official_email"
    payload["gaca_email_message_id"] = delivery.message_id
    payload["gaca_email_accepted_at"] = int(time.time())
    payload["gaca_portal_submission_verified"] = False
    recovery = (
        " A matching copy was already present in Gmail Sent, so FlightDeck "
        "did not send a duplicate."
        if delivery.recovered_from_sent else ""
    )
    return PortalResult(
        "needs_attention",
        "GACA's official email server accepted an audit copy, but this is "
        "not a verified portal submission. The GACA portal job remains "
        "queued until a real regulator reference is confirmed." + recovery,
        retry_safe=True,
        error_code=result.error_code,
    )


def _start_portal_worker(
        job: dict,
        on_complete: Callable[[PortalResult], None] | None = None,
        on_update: Callable[[str, str, bytes | None], None]
        | None = None) -> None:
    """Run one already-persisted and atomically leased portal job."""
    job_id = str(job["id"])
    payload = dict(job.get("payload") or {})
    # Legacy durable rows may have persisted the routing columns before the
    # full JSON payload migration.  Always restore those authoritative fields
    # from the job itself so a retry cannot crash on payload["kind"] or lose
    # its complaint/flight identity.
    for key in ("kind", "airline_code", "flight_number", "flight_key"):
        if job.get(key) not in ("", None):
            payload.setdefault(key, job.get(key))
    if job.get("complaint_id") is not None:
        payload.setdefault("portal_complaint_id", job.get("complaint_id"))
    state = {"last_stage": str(job.get("status") or "leased")}
    with _JOBS_LOCK:
        _JOBS[job_id] = dict(job, payload=payload, terminal=False)

    def update(status: str, message: str, image: bytes | None = None):
        state["last_stage"] = status
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
            error_code = str(getattr(exc, "code", "") or "")
            result = PortalResult(
                "error",
                f"Portal automation stopped: {exc}",
                retry_safe=bool(error_code),
                error_code=error_code,
            )
        result = _maybe_use_gaca_email_fallback(
            job_id, payload, result, update)
        previous_stage = state["last_stage"]
        gaca_reconciliation_retry = (
            payload.get("kind") == "gaca"
            and result.status not in {
                "submitted", "success", "accepted_pending_reference",
            }
            and not re.search(r"\bcancel(?:led|ed)?\b", result.message, re.I)
        )
        if _retry_is_safe(result, previous_stage) or gaca_reconciliation_retry:
            remediated_proxy = bool(re.search(
                r"rotated (?:the )?GACA residential proxy|"
                r"GACA residential proxy was rotated",
                str(result.message or ""),
                re.I,
            ))
            gaca_rate_limited = bool(re.search(
                r"GACA[\s\S]{0,160}too many submission attempts",
                str(result.message or ""),
                re.I,
            )) or result.error_code == "gaca_identity_rate_limited"
            gaca_waf_blocked = (
                result.error_code == "gaca_waf_blocked"
                or (
                    remediated_proxy
                    and bool(re.search(
                        r"\bWAF\b|HTTP\s*403|blocked",
                        str(result.message or ""),
                        re.I,
                    ))
                )
            )
            gaca_form_not_ready = bool(re.search(
                r"GACA (?:Gender|Country Code) could not be set",
                str(result.message or ""),
                re.I,
            ))
            if gaca_rate_limited:
                raw_delay = os.environ.get(
                    "FLIGHTBOT_GACA_RATE_LIMIT_SECONDS", "86400").strip()
                raw_spacing = os.environ.get(
                    "FLIGHTBOT_GACA_RATE_LIMIT_SPACING_SECONDS",
                    "86400",
                ).strip()
                try:
                    circuit_delay = int(raw_delay)
                except (TypeError, ValueError):
                    circuit_delay = 24 * 3600
                try:
                    circuit_spacing = int(raw_spacing)
                except (TypeError, ValueError):
                    circuit_spacing = 24 * 3600
                next_attempt = db.defer_gaca_identity_jobs(
                    job_id,
                    result.message,
                    payload=payload,
                    delay=circuit_delay,
                    spacing=circuit_spacing,
                )
            elif gaca_waf_blocked:
                rotated = bool(payload.get("_gaca_waf_rotated"))
                raw_delay = os.environ.get(
                    "FLIGHTBOT_GACA_WAF_RETRY_SECONDS",
                    "300" if rotated else "3600",
                ).strip()
                try:
                    waf_delay = int(raw_delay)
                except (TypeError, ValueError):
                    waf_delay = 5 * 60 if rotated else 60 * 60
                try:
                    consecutive_waf_blocks = max(
                        1, int(payload.get("_gaca_waf_rejections") or 1))
                except (TypeError, ValueError):
                    consecutive_waf_blocks = 1
                waf_delay *= 2 ** min(consecutive_waf_blocks - 1, 3)
                next_attempt = db.retry_portal_job(
                    job_id,
                    result.message,
                    fixed_delay=max(90, min(waf_delay, 60 * 60)),
                )
            else:
                next_attempt = db.retry_portal_job(
                    job_id,
                    result.message,
                    minimum_delay=_retry_minimum_delay(result),
                    fixed_delay=(
                        90
                        if remediated_proxy or gaca_form_not_ready
                        else None
                    ),
                )
            if next_attempt is not None:
                delay_minutes = max(
                    1, round((next_attempt - time.time()) / 60))
                refreshed = db.get_portal_job(job_id) or {}
                with _JOBS_LOCK:
                    current = _JOBS.get(job_id)
                    if current:
                        current.update({
                            key: refreshed.get(key) for key in (
                                "attempts", "max_attempts", "next_attempt_at",
                                "lease_until", "last_error")
                        })
                update(
                    "retry_wait",
                    f"{result.message} This failed before a confirmed Submit "
                    f"and is queued to retry in about {delay_minutes} minutes.")
                return
            result = PortalResult(
                "quarantined",
                f"{result.message} The safe retry limit was reached and the "
                "job was quarantined for manual review.")
        elif (result.status not in {
                "submitted", "accepted_pending_reference"}
              and previous_stage == "submitting"):
            db.quarantine_portal_job(job_id, result.message)
            result = PortalResult(
                "quarantined",
                f"{result.message} The attempt may have reached Submit, so it "
                "was quarantined pending email/SMS/portal reconciliation.")
        update(result.status, result.message)
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(
                    reference=result.reference,
                    lease_until=None,
                    next_attempt_at=None)
                snapshot = dict(current)
            else:
                snapshot = None
        if snapshot:
            try:
                db.save_portal_job(snapshot)
            except Exception:
                logger.exception("Could not persist final portal job %s", job_id)
        try:
            _finalize_persisted_complaint(payload, result)
        except Exception:
            logger.exception(
                "Could not apply durable portal result for job %s", job_id)
        if on_complete:
            try:
                on_complete(result)
            except Exception:
                logger.exception("Portal completion callback failed for job %s",
                                 job_id)

    threading.Thread(target=worker, name=f"portal-{job_id[:8]}",
                     daemon=True).start()


def start_portal_job(payload: dict,
                     on_complete: Callable[[PortalResult], None] | None = None,
                     on_update: Callable[[str, str, bytes | None], None]
                     | None = None) -> str:
    """Persist, lease, and start one portal submission."""
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "kind": payload.get("kind") or "",
        "airline_code": payload.get("airline_code") or "",
        "flight_number": payload.get("flight_number") or "",
        "flight_key": payload.get("flight_key") or "",
        "complaint_id": payload.get("portal_complaint_id"),
        "status": "queued",
        "message": "Preparing the official complaint portal…",
        "reference": "",
        "terminal": False,
        "attempts": 0,
        # Confirmed pre-Submit failures remain durable work. Backoff is capped
        # in the database, so a long outage does not turn into rapid retries.
        # Ambiguous post-Submit attempts still use the separate quarantine path.
        "max_attempts": 100000,
        "payload": payload,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    db.save_portal_job(job)
    identity_circuit_until = db.apply_gaca_identity_circuit(job_id)
    if identity_circuit_until > time.time():
        refreshed = db.get_portal_job(job_id) or {}
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update(refreshed)
        wait_hours = max(
            1, round((identity_circuit_until - time.time()) / 3600))
        if on_update:
            on_update(
                "retry_wait",
                "This passenger is temporarily paused by GACA's identity "
                f"limit. The saved complaint will retry in about "
                f"{wait_hours} hours; other passengers are unaffected.",
                None,
            )
        return job_id
    claimed = db.claim_portal_job(job_id)
    if not claimed:
        raise RuntimeError("Could not lease the persisted portal job")
    _start_portal_worker(
        claimed, on_complete=on_complete, on_update=on_update)
    return job_id


def resume_due_portal_jobs(
        on_update_factory: Callable[[], Callable] | None = None,
        limit: int = 1) -> list[str]:
    """Resume due pre-submit failures from their persisted payloads."""
    started = []
    for pending in db.list_due_portal_jobs(limit):
        claimed = db.claim_portal_job(str(pending["id"]))
        if not claimed:
            continue
        callback = on_update_factory() if on_update_factory else None
        _start_portal_worker(claimed, on_update=callback)
        started.append(str(claimed["id"]))
    return started


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


def _attach_gaca_network_diagnostics(page) -> None:
    """Capture GACA POST outcomes and optionally log safe diagnostics."""
    debug_enabled = os.environ.get(
        "FLIGHTBOT_GACA_DEBUG_NETWORK", "").strip().casefold() in {
            "1", "true", "yes", "on"}

    def relevant(url: str) -> bool:
        value = str(url or "").casefold()
        return (
            "gaca.gov.sa" in value
            and any(part in value for part in (
                "complaint-airline", "category", "subcategory",
                "verification"))
        )

    def on_response(response) -> None:
        try:
            if relevant(response.url):
                if str(response.request.method).upper() == "POST":
                    try:
                        response_headers = response.headers or {}
                    except Exception:
                        response_headers = {}
                    location = str(response_headers.get("location") or "")
                    setattr(
                        page,
                        "_flightdeck_gaca_last_post_status",
                        int(response.status),
                    )
                    setattr(
                        page,
                        "_flightdeck_gaca_last_post_url",
                        str(response.url or ""),
                    )
                    setattr(
                        page,
                        "_flightdeck_gaca_last_post_location",
                        location,
                    )
                if not debug_enabled:
                    return
                logger.warning(
                    "GACA network response HTTP %s %s %s location=%s",
                    response.status,
                    response.request.method,
                    response.url,
                    str(getattr(
                        page,
                        "_flightdeck_gaca_last_post_location",
                        "",
                    ) or ""),
                )
                if str(response.request.method).upper() == "POST":
                    fields = parse_qs(
                        str(response.request.post_data or ""),
                        keep_blank_values=True,
                    )
                    field_shape = {
                        key: [len(str(value)) for value in values]
                        for key, values in fields.items()
                    }
                    try:
                        headers = response.request.all_headers() or {}
                    except Exception:
                        headers = response.request.headers or {}
                    safe_headers = {
                        key: headers.get(key, "")
                        for key in ("content-type", "origin", "referer")
                    }
                    cookie_shape = []
                    for cookie in str(
                            headers.get("cookie") or "").split(";"):
                        name, separator, value = cookie.strip().partition("=")
                        if not separator:
                            continue
                        cookie_shape.append({
                            "name": name,
                            "hash": hashlib.sha256(
                                value.encode("utf-8")).hexdigest()[:12],
                        })
                    csrf_hashes = [
                        hashlib.sha256(
                            str(value).encode("utf-8")).hexdigest()[:12]
                        for value in fields.get("_csrf", [])
                    ]
                    logger.warning(
                        "GACA POST shape fields=%s csrfHashes=%s "
                        "cookies=%s headers=%s",
                        json.dumps(field_shape, ensure_ascii=False),
                        json.dumps(csrf_hashes),
                        json.dumps(cookie_shape, ensure_ascii=False),
                        json.dumps(safe_headers, ensure_ascii=False),
                    )
                if (response.status >= 400
                        and str(response.request.method).upper() == "POST"):
                    try:
                        response_excerpt = re.sub(
                            r"\s+", " ", response.text())[:1000]
                    except Exception:
                        response_excerpt = ""
                    logger.warning(
                        "GACA rejected POST response=%s",
                        response_excerpt,
                    )
        except Exception:
            pass

    def on_failed(request) -> None:
        try:
            if debug_enabled and relevant(request.url):
                logger.warning(
                    "GACA network request failed %s %s: %s",
                    request.method,
                    request.url,
                    request.failure,
                )
        except Exception:
            pass

    page.on("response", on_response)
    page.on("requestfailed", on_failed)


def _install_gaca_https_upgrade(context) -> None:
    """Preserve GACA's secure wizard session across its HTTP redirect typo.

    GACA responds to successful wizard POSTs with an ``http://`` Location.
    Established browsers silently upgrade it through HSTS. A fresh automation
    profile can make the insecure request first, which omits the Secure
    JSESSIONID and causes GACA to create a different session before the next
    step. Upgrade the request inside Chromium before it reaches the network.
    """
    def upgrade(route) -> None:
        url = str(route.request.url or "")
        if url.casefold().startswith("http://myeservices.gaca.gov.sa/"):
            if os.environ.get(
                    "FLIGHTBOT_GACA_DEBUG_NETWORK",
                    "").strip().casefold() in {"1", "true", "yes", "on"}:
                logger.warning(
                    "Upgrading GACA wizard redirect to HTTPS before network: %s",
                    url)
            route.continue_(url="https://" + url[len("http://"):])
        else:
            route.continue_()

    # Match every browser request here and decide inside the handler. Chromium
    # did not consistently pass redirect navigations through a regex-scoped
    # context route (the HTTP hop was still visible in the network log). The
    # broad route reliably sees top-level redirect requests as well as assets.
    context.route("**/*", upgrade)


def _launch_context(playwright, *, proxy_server: str = "",
                    profile_dir: Path | None = None,
                    block_service_workers: bool = False):
    profile_dir = profile_dir or _PROFILE_DIR
    profile_dir.mkdir(exist_ok=True)
    configured = os.environ.get("FLIGHTBOT_HEADLESS_BROWSER", "").strip().lower()
    headless = (configured in {"1", "true", "yes", "on"}
                if configured else sys.platform != "win32")
    browser_args = ["--disable-blink-features=AutomationControlled"]
    live_cdp_port = os.environ.get(
        "FLIGHTBOT_GACA_LIVE_CDP_PORT", "").strip()
    if re.fullmatch(r"\d{2,5}", live_cdp_port):
        browser_args.extend((
            f"--remote-debugging-port={live_cdp_port}",
            "--remote-allow-origins=*",
        ))
    options = dict(user_data_dir=str(profile_dir), headless=headless,
                   viewport={"width": 1360, "height": 900},
                   locale="en-US", timezone_id="Asia/Riyadh",
                   args=browser_args)
    if block_service_workers:
        options["service_workers"] = "block"
    # Use the installed browser's real user agent by default. A fixed
    # Windows/old-Chrome UA on a current Linux browser creates a fingerprint
    # mismatch that strict regulator WAFs can reject.
    configured_user_agent = os.environ.get(
        "FLIGHTBOT_BROWSER_USER_AGENT", "").strip()
    if configured_user_agent:
        options["user_agent"] = configured_user_agent
    proxy = (proxy_server or "").strip()
    if proxy:
        options["proxy"] = _playwright_proxy_options(proxy)
    context = None
    preferred_channel = os.environ.get(
        "FLIGHTBOT_BROWSER_CHANNEL", "").strip()
    if not preferred_channel:
        preferred_channel = "msedge" if sys.platform == "win32" else "chrome"
    if preferred_channel:
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=preferred_channel, **options)
        except Exception:
            logger.warning(
                "Installed browser channel %s was unavailable; falling back "
                "to Playwright Chromium.", preferred_channel, exc_info=True)
    if context is None:
        context = playwright.chromium.launch_persistent_context(**options)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return context


def _playwright_proxy_options(proxy_url: str) -> dict:
    """Split proxy credentials from the server URL for Chromium.

    Curl accepts ``http://user:pass@host:port`` directly, while Chromium
    launched through Playwright can reject the same URL with
    ERR_INVALID_AUTH_CREDENTIALS. Playwright expects credentials in their own
    fields.
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname:
        return {"server": raw}
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parsed.scheme or 'http'}://{host}"
    if parsed.port:
        server += f":{parsed.port}"
    options = {"server": server}
    if parsed.username is not None:
        options["username"] = unquote(parsed.username)
    if parsed.password is not None:
        options["password"] = unquote(parsed.password)
    return options


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


def _match_select_option(select, choices: list[str]) -> str:
    """Return the exact option label matching one of the choice patterns."""
    try:
        options = select.locator("option").all_text_contents()
    except Exception:
        return ""
    for choice in choices:
        match = next((option for option in options
                      if re.search(choice, (option or "").strip(), re.I)), None)
        if match and str(match).strip():
            return str(match).strip()
    return ""


def _select_native_option(select, choices: list[str]) -> bool:
    """Select a native <option>, including for selectize-hidden controls."""
    match = _match_select_option(select, choices)
    if not match:
        return False
    try:
        select.select_option(label=match)
    except Exception:
        try:
            # Some GACA selects expose Arabic/English values separately.
            select.select_option(value=match)
        except Exception:
            return False
    try:
        select.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
    except Exception:
        pass
    return True


def _select(page, labels: list[str], choices: list[str],
            queries: list[str] | None = None) -> bool:
    choices = [choice for choice in choices if choice]
    if not choices:
        return False
    for label in labels:
        locator = page.get_by_label(re.compile(label, re.I))
        if not locator.count():
            continue
        # Prefer a visible control, but still try a labeled hidden <select>
        # (GACA wraps some required fields with Selectize).
        try:
            target = locator.first
            if _select_native_option(target, choices):
                return True
        except Exception:
            pass
    try:
        selects = page.locator("select").all()
    except Exception:
        selects = []
    for select in selects:
        try:
            # Match by nearby label text when the native select is hidden.
            label_text = select.evaluate("""el => {
                const labels = el.labels
                    ? Array.from(el.labels).map(x => x.innerText).join(' ')
                    : '';
                const wrap = el.closest('.form-group, .mb-3, .field, .row')
                    || el.parentElement;
                return (labels || wrap?.innerText || el.name || el.id || '')
                    .slice(0, 240);
            }""")
        except Exception:
            label_text = ""
        labeled = any(re.search(label, str(label_text or ""), re.I)
                      for label in labels)
        try:
            shown = select.is_visible()
        except Exception:
            shown = False
        # Hidden selects are only safe when their label/name clearly matches.
        # Visible selects keep the legacy "any matching option" fallback.
        if not labeled and not shown:
            continue
        if labeled or shown:
            if _select_native_option(select, choices):
                return True
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


def _material_dropdown_options(page, labels: list[str]) -> list[str]:
    """Read the exact options currently rendered by a Material dropdown."""
    try:
        material_fields = page.locator("mat-form-field").all()
    except Exception:
        return []
    for field in material_fields:
        opened = False
        try:
            if not field.is_visible():
                continue
            label = field.locator("mat-label")
            label_text = (label.first.inner_text() if label.count()
                          else field.inner_text())
            label_text = re.sub(r"\s+", " ", label_text).strip()
            if not any(re.search(pattern, label_text, re.I)
                       for pattern in labels):
                continue
            control = field.locator("mat-select")
            if not _visible(control):
                continue
            control.first.click(force=True)
            opened = True
            page.wait_for_timeout(250)
            options = page.locator("mat-option, [role='option']")
            rendered = []
            for index in range(options.count()):
                option = options.nth(index)
                if not option.is_visible():
                    continue
                text = re.sub(r"\s+", " ", option.inner_text()).strip()
                if (text and text.casefold() not in {"please select", "select"}
                        and text not in rendered):
                    rendered.append(text)
            return rendered
        except Exception:
            continue
        finally:
            if opened:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    return []


def _choose_saudia_category(payload: dict,
                             options: list[str]) -> tuple[str, bool]:
    """Ask Ghala to choose an exact live option, with a bounded fallback."""
    rendered = [" ".join(str(option or "").split()).strip()
                for option in options]
    rendered = list(dict.fromkeys(option for option in rendered if option))
    if _CATEGORY_HANDLER and rendered:
        try:
            flight = {
                key: payload.get(key) for key in (
                    "airline_code", "flight_number", "flight_date",
                    "origin", "destination")
                if payload.get(key) not in (None, "", [])
            }
            decision = _CATEGORY_HANDLER(
                str(payload.get("incident") or ""), rendered,
                payload.get("ai_analysis") or {}, flight) or {}
            requested = str(decision.get("category") or "").strip()
            exact = next((option for option in rendered
                          if option.casefold() == requested.casefold()), "")
            if exact:
                return exact, True
            logger.warning(
                "Ghala returned no valid category from the live Saudia options")
        except Exception:
            logger.exception("Ghala category selection failed")

    text = str(payload.get("incident") or "").casefold()
    option_patterns = []
    if re.search(r"\b(?:bag|bags|baggage|luggage|suitcase|suitcases)\b", text):
        # Prefer a newly exposed baggage-specific option over the historical
        # generic Saudia fallback when Ghala is temporarily unavailable.
        specific = next((option for option in rendered
                         if re.search(r"bag|luggage", option, re.I)), "")
        if specific:
            return specific, False
        option_patterns.append(r"quality|service")
    elif re.search(r"\bcancel(?:led|ed|lation)?\b", text):
        option_patterns.append(r"cancel")
    elif re.search(r"\b(?:delay|delayed|late)\b", text):
        option_patterns.append(r"delay")
    preferred = _saudia_complaint_category(payload)
    exact = next((option for option in rendered
                  if option.casefold() == preferred.casefold()), "")
    if exact:
        return exact, False
    option_patterns.extend((r"quality\s+of\s+services?", r"service"))
    for pattern in option_patterns:
        match = next((option for option in rendered
                      if re.search(pattern, option, re.I)), "")
        if match:
            return match, False
    return (rendered[0] if rendered else preferred), False


def _select_saudia_complaint_category(page, payload: dict, update) -> str:
    labels = [r"^complaint\s*\*?$"]
    options = _material_dropdown_options(page, labels)
    if options and _CATEGORY_HANDLER:
        update(
            "filling",
            f"Ghala is choosing the best category from Saudia's "
            f"{len(options)} current dropdown options…")
    category, used_ai = _choose_saudia_category(payload, options)
    if not _select(page, labels, [rf"^{re.escape(category)}$"]):
        raise RuntimeError(
            f"Saudia's production form did not accept the '{category}' "
            "complaint category; nothing was submitted.")
    payload["selected_complaint_category"] = category
    source = "Ghala selected" if used_ai else "Selected"
    update("filling", f"{source} Saudia category: {category}.")
    return category


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
    blob = f"{title}\n{text}"
    return bool(re.search(
        r"the request is blocked|pardon our interruption|access denied|"
        r"request (?:was )?rejected|service unavailable|"
        r"there is an error loading this page|"
        r"this page can'?t be displayed|incident id\s*:"
        r"|contact support for additional information",
        blob, re.I))


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
    pending human step. Invisible reCAPTCHA often has no checked checkbox;
    treat a non-empty g-recaptcha-response (or patched getResponse) as done."""
    try:
        patched = page.evaluate("""() => {
            try {
                const value = window.grecaptcha?.getResponse?.()
                    || window.grecaptcha?.enterprise?.getResponse?.();
                return !!(value && String(value).trim());
            } catch (_) { return false; }
        }""")
        if patched:
            return True
    except Exception:
        pass
    for frame in getattr(page, "frames", []):
        url = str(getattr(frame, "url", ""))
        try:
            if "recaptcha" in url and "anchor" in url:
                # Invisible widgets never flip aria-checked; skip them here.
                if "size=invisible" in url:
                    continue
                anchor = frame.locator("#recaptcha-anchor")
                present = anchor.count() if hasattr(anchor, "count") else 1
                if (present and anchor.get_attribute(
                        "aria-checked") == "true"):
                    return True
            if "hcaptcha.com" in url and "checkbox" in url:
                checkbox = frame.locator("#checkbox")
                present = checkbox.count() if hasattr(checkbox, "count") else 1
                if (present and checkbox.get_attribute(
                        "aria-checked") == "true"):
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


def _recaptcha_page_context(page, site_key: str) -> dict:
    """Detect execute-based v3 pages and preserve their server-checked action."""
    try:
        context = page.evaluate("""siteKey => {
            // RECAPTCHA_PAGE_CONTEXT
            let isV3 = false;
            let isEnterprise = false;
            for (const script of Array.from(document.scripts)) {
                const src = script.src || "";
                if (!/recaptcha\\/(?:api|enterprise)\\.js/i.test(src)) continue;
                let render = "";
                try { render = new URL(src).searchParams.get("render") || ""; }
                catch (_) {}
                if (render && render !== "explicit"
                        && (!siteKey || render === siteKey)) {
                    isV3 = true;
                    isEnterprise = /enterprise\\.js/i.test(src);
                    break;
                }
            }
            if (!isV3) return {is_v3: false};
            const form = Array.from(document.forms).find(item =>
                item.hasAttribute("data-recaptcha")
                || item.classList.contains("g-recaptcha-form"));
            return {
                is_v3: true,
                is_enterprise: isEnterprise,
                page_action: form?.dataset?.recaptchaAction || "submit",
            };
        }""", site_key)
    except Exception:
        return {}
    return context if isinstance(context, dict) else {}


def _with_recaptcha_page_context(page, challenge: dict) -> dict:
    context = _recaptcha_page_context(page, str(challenge.get("site_key") or ""))
    if not context.get("is_v3"):
        return challenge
    challenge.update({
        "is_v3": True,
        "is_invisible": True,
        "is_enterprise": bool(
            context.get("is_enterprise") or challenge.get("is_enterprise")),
        "page_action": str(context.get("page_action") or "submit"),
        # GACA's server rejected both native/low-score tokens and 0.7 solver
        # tokens. Request 2Captcha's highest supported v3 tier for GACA;
        # other portals retain the standard 0.3 floor.
        "min_score": (
            0.9 if "gaca.gov.sa" in str(challenge.get("website_url") or "")
            else 0.3),
    })
    return challenge


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
        # GACA's v3 page can expose the site key only in
        # api.js?render=<site-key>.  The iframe is created lazily by
        # grecaptcha.execute(), which is too late for the solver discovery
        # pass.  Recover the public key directly from the rendered script URL.
        script_site_key = ""
        script_api_domain = "google.com"
        try:
            sources = page.locator(
                "script[src*='recaptcha'][src*='render=']").evaluate_all(
                    "scripts => scripts.map(script => script.src || '')")
        except Exception:
            sources = []
        for source in sources or []:
            try:
                parsed_script = urlparse(str(source or ""))
                render = str(
                    (parse_qs(parsed_script.query).get("render") or [""])[0]
                ).strip()
            except Exception:
                continue
            if not render or render.casefold() == "explicit":
                continue
            script_site_key = render
            script_api_domain = (
                "recaptcha.net"
                if (parsed_script.hostname or "").endswith("recaptcha.net")
                else "google.com"
            )
            break

        # Angular can alternatively expose its site key on a widget before
        # Google's iframe is attached.
        try:
            widget = page.locator(
                ".g-recaptcha[data-sitekey], [data-sitekey]").first
            site_key = str(widget.get_attribute("data-sitekey") or "").strip()
            data_s = str(widget.get_attribute("data-s") or "").strip()
        except Exception:
            site_key = ""
            data_s = ""
        if not site_key and script_site_key:
            site_key = script_site_key
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
            "is_invisible": bool(script_site_key),
            "is_enterprise": False,
            "user_agent": user_agent,
            "api_domain": script_api_domain,
        }
        if data_s:
            challenge["data_s"] = data_s
        return _with_recaptcha_page_context(page, challenge)
    _score, _position, parsed, params, site_key, data_s = max(candidates)
    try:
        user_agent = str(page.evaluate("navigator.userAgent") or "")
    except Exception:
        user_agent = _CHROME_USER_AGENT
    challenge = {
        "kind": "recaptcha",
        "website_url": page.url,
        "site_key": site_key,
        "is_invisible": (
            (params.get("size") or [""])[0] == "invisible"
            or "size=invisible" in parsed.query
            or "size=invisible" in (parsed.path + "?" + parsed.query)),
        "is_enterprise": "/enterprise/" in parsed.path,
        "user_agent": user_agent,
        "api_domain": ("recaptcha.net"
                       if (parsed.hostname or "").endswith("recaptcha.net")
                       else "google.com"),
    }
    if data_s:
        challenge["data_s"] = data_s
    return _with_recaptcha_page_context(page, challenge)


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
    """Apply a solved token, including invisible reCAPTCHA execute hooks.

    GACA (and similar portals) call grecaptcha.execute() on Submit. Without
    patching getResponse/execute, that call starts a fresh challenge and
    discards the token 2Captcha just returned.
    """
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
                try { field.innerHTML = token; } catch (_) {}
                field.dispatchEvent(new Event("input", {bubbles: true}));
                field.dispatchEvent(new Event("change", {bubbles: true}));
            }
            // v3 pages commonly create this input only inside their Submit
            // handler. Ensure the solved token is actually part of the
            // complaint form before that handler gets a chance to replace it.
            const targetForm = document.querySelector(
                "form.g-recaptcha-form[data-recaptcha], " +
                "form.g-recaptcha-form, form[data-recaptcha]");
            if (targetForm) {
                let formField = targetForm.querySelector(
                    "[name='g-recaptcha-response']");
                if (!formField) {
                    formField = document.createElement("input");
                    formField.type = "hidden";
                    formField.name = "g-recaptcha-response";
                    targetForm.appendChild(formField);
                    fields.push(formField);
                }
                formField.value = token;
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

            const patchApi = (api) => {
                if (!api || typeof api !== "object") return 0;
                let patched = 0;
                try {
                    api.getResponse = function () { return token; };
                    patched += 1;
                } catch (_) {}
                try {
                    api.execute = function () {
                        for (const callback of callbacks) {
                            try { callback(token); } catch (_) {}
                        }
                        return Promise.resolve(token);
                    };
                    patched += 1;
                } catch (_) {}
                return patched;
            };
            const patched = patchApi(window.grecaptcha)
                + patchApi(window.grecaptcha?.enterprise);

            let called = 0;
            for (const callback of callbacks) {
                try { callback(token); called += 1; } catch (_) {}
            }
            return {
                fields: fields.length,
                callbacks: called,
                patched,
                token_len: (token || "").length,
            };
        }""", token)
    except Exception:
        return False
    return bool(
        (applied or {}).get("fields")
        or (applied or {}).get("callbacks")
        or (applied or {}).get("patched"))


def _submit_gaca_form_with_injected_token(form) -> bool:
    """Post GACA once with the solver token already inside the form.

    The portal's document-level Submit handler executes reCAPTCHA again and
    replaces the externally solved token. Native ``form.submit()`` bypasses
    that handler while retaining the action, CSRF query, and multipart fields.
    """
    try:
        return bool(form.evaluate("""f => {
            if (!f.checkValidity()) return false;
            const token = f.querySelector("[name='g-recaptcha-response']");
            if (!token || !String(token.value || '').trim()) return false;
            HTMLFormElement.prototype.submit.call(f);
            return true;
        }"""))
    except Exception:
        return False


def _settle_gaca_native_recaptcha(page, update) -> None:
    """Let GACA's v3 page finish loading before its one native execution."""
    raw = os.environ.get(
        "FLIGHTBOT_GACA_NATIVE_SETTLE_SECONDS", "18").strip()
    try:
        seconds = max(8, min(int(raw), 45))
    except (TypeError, ValueError):
        seconds = 18
    update(
        "verification",
        "The final GACA page is complete. Allowing its native security "
        "verification to finish establishing before the single Submit…",
        _page_screenshot(page))
    started = time.monotonic()
    try:
        page.bring_to_front()
        page.mouse.move(180, 180, steps=8)
        page.mouse.wheel(0, 420)
        page.wait_for_timeout(900)
        page.mouse.move(720, 510, steps=14)
        details = page.locator(
            "textarea#complaintDetails, textarea[name='complaintDetails']")
        if details.count() and details.first.is_visible():
            details.first.click(position={"x": 60, "y": 18})
            page.keyboard.press("End")
        page.wait_for_timeout(900)
        page.mouse.wheel(0, 520)
        page.wait_for_timeout(900)
        page.mouse.move(980, 720, steps=16)
    except Exception:
        logger.debug(
            "Could not complete every GACA final-page settle gesture",
            exc_info=True)
    remaining = seconds - (time.monotonic() - started)
    if remaining > 0:
        page.wait_for_timeout(int(remaining * 1000))


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
    # Invisible reCAPTCHA needs a beat for patched execute/getResponse hooks
    # and Angular change detection before Submit is clicked.
    page.wait_for_timeout(1200)
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
    if _visible(_otp_fields(page)):
        return "Enter the OTP sent by the official portal."
    checks = (
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


def _otp_fields(page):
    """Locate conventional OTP inputs and GACA's one-box-per-digit UI."""
    try:
        named = page.get_by_role(
            "textbox", name=re.compile(r"digit of verification code", re.I))
        if _visible(named):
            return named
    except Exception:
        pass
    return page.locator(
        "input[name*='otp' i], input[id*='otp' i], "
        "input[autocomplete='one-time-code'], "
        "input[aria-label*='verification code' i]")


def _page_screenshot(page) -> bytes:
    try:
        return page.screenshot(type="png", full_page=False)
    except Exception:
        return b""


def _ask_verification(kind: str, message: str, page, image: bytes = b"",
                      choices: list[str] | None = None,
                      context: dict | None = None):
    if not _VERIFICATION_HANDLER:
        return None
    challenge = {
        "kind": kind,
        "message": message,
        "image": image or _page_screenshot(page),
        "choices": choices or [],
        "url": page.url,
    }
    challenge.update(context or {})
    return _VERIFICATION_HANDLER(challenge)


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
        # Gender is derived from title and may not appear as an exact payload
        # string when Ghala only returns Male/Female.
        if re.search(r"gender|\bsex\b", target, re.I):
            allowed.update({"Male", "Female", "ذكر", "أنثى", "انثى"})
            if not value:
                value = str(payload.get("gender") or "").strip() or (
                    "Male" if str(payload.get("title") or "").casefold()
                    in {"mr", "mr.", "mister"} else "")
        if value not in allowed:
            return False, False
        labels = [re.escape(target)]
        if action == "fill":
            changed = _fill(page, labels, value)
        elif re.search(r"nationality", target, re.I):
            choices, queries = _nationality_selection(value)
            changed = _select(page, labels, choices, queries=queries)
        elif re.search(r"gender|\bsex\b", target, re.I):
            changed = _select_gaca_gender(page, {**payload, "gender": value})
            if not changed:
                changed = _select(page, ["gender", r"^sex$"],
                                  [rf"^{re.escape(value)}$", re.escape(value)])
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


def _solve_otp(page, update, recipient_email: str = "") -> bool:
    fields = _otp_fields(page)
    if not _visible(fields):
        return False
    response = _ask_verification(
        "otp",
        "Reply with the one-time code sent by the official portal.",
        page,
        context={"recipient_email": recipient_email},
    )
    code = re.sub(r"\D", "", str(response or ""))
    if not code:
        return False
    visible_fields = []
    for index in range(fields.count()):
        field = fields.nth(index)
        try:
            if field.is_visible():
                visible_fields.append(field)
        except Exception:
            continue
    if len(visible_fields) > 1:
        if len(code) < len(visible_fields):
            return False
        if (
            gaca_normal_browser.os_input_enabled()
            and "myeservices.gaca.gov.sa" in str(page.url)
        ):
            verify = page.get_by_role(
                "button", name=re.compile(r"^Verify$", re.I))
            if not _visible(verify):
                return False
            gaca_normal_browser.physical_type_otp(
                page, visible_fields, code, verify.first)
        else:
            for field, digit in zip(visible_fields, code):
                field.fill(digit)
            _click(page, [
                "Verify", "Continue", "Confirm",
                "تحقق", "متابعة", "تأكيد"])
    else:
        (visible_fields[0] if visible_fields else fields.first).fill(code)
        _click(page, [
            "Verify", "Continue", "Confirm",
            "تحقق", "متابعة", "تأكيد"])
    page.wait_for_timeout(1200)
    update("filling", "OTP entered. Continuing…")
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
                         defer_captcha: bool = False,
                         recipient_email: str = "") -> bool:
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
    otp_started = _visible(_otp_fields(page))
    if _VERIFICATION_HANDLER and _solve_otp(
            page, update, recipient_email):
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
        # An OTP redirect can land on a fresh CAPTCHA or the final form after a
        # rejection. Return control to the confirmation state machine as soon
        # as the OTP controls disappear so it can inspect the POST response.
        if otp_started and not _visible(_otp_fields(page)):
            update("filling", "Email verification response received. Checking it…")
            return True
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
    if payload.get("kind") == "gaca":
        main, sub, detail = _gaca_categories(payload)
        if re.search(r"main\s*category", attributes, re.I):
            return main
        if re.search(r"sub-?subcategory", attributes, re.I):
            return detail
        if re.search(r"\bsubcategory\b", attributes, re.I):
            return sub
    mappings = (
        (r"\bemail\b|e-mail", "email"),
        (r"country.{0,20}(?:code|territory)", "country_code"),
        (r"\b(?:mobile|phone|telephone)\b", "phone"),
        (r"\b(?:first|given).{0,10}name\b", "first_name"),
        (r"\b(?:second|middle).{0,10}name\b", "middle_name"),
        (r"\b(?:last|family).{0,10}name\b|\bsurname\b", "last_name"),
        (r"\bgender\b|\bsex\b", "gender"),
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
        if known_value:
            restored = False
            if (payload and payload.get("kind") == "gaca"
                    and re.search(r"category|subcategory|gender|country", label, re.I)):
                restored = _select_gaca_dropdown(
                    page, re.escape(re.sub(r"[*:\s]+$", "", label).strip()),
                    known_value)
                if not restored and re.search(r"gender", label, re.I):
                    restored = _select_gaca_gender(
                        page, {**payload, "gender": known_value})
                if not restored and re.search(r"country", label, re.I):
                    restored = _selectize_by_label(
                        page, r"country\s*code", known_value,
                        [re.escape(known_value), r"Saudi Arabia.*\+966", r"\+966"])
            if not restored:
                restored = _apply_control_answer(control, known_value)
            if restored:
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
        # IFE / broken screen must beat the word "seat" in phrases like
        # "seat-back entertainment" or "screen at my seat".
        (r"entertainment|\bife\b|in.?flight (?:system|movie)|(?:broken|not working|faulty).{0,40}screen|screen.{0,40}(?:broken|not working|faulty)|accessibility|wheelchair",
         "Quality of services"),
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
        (r"screen|refund|damag|lost", "Quality of services"),
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
    _select_saudia_complaint_category(page, payload, update)
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
    explicit = payload.get("gaca_category") or payload.get("portal_category")
    if isinstance(explicit, dict):
        parts = [
            explicit.get("main"),
            explicit.get("sub"),
            explicit.get("detail"),
        ]
    elif isinstance(explicit, (list, tuple)):
        parts = list(explicit)
    elif isinstance(explicit, str) and explicit.strip():
        parts = re.split(r"\s*(?:›|>|/)\s*", explicit.strip())
    else:
        parts = []
    if parts:
        normalized = [str(value or "").strip() for value in parts[:3]]
        normalized.extend([""] * (3 - len(normalized)))
        if normalized[0]:
            return tuple(normalized)

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
        (r"damag\w*.{0,40}(?:bag|baggage|luggage)|(?:bag|baggage|luggage).{0,40}damag",
         ("Baggage Services", "Damage Baggage", "")),
        (r"delay\w*.{0,40}(?:bag|baggage|luggage)|(?:bag|baggage|luggage).{0,40}delay",
         ("Baggage Services", "Baggage Delay", "")),
        (r"lost.{0,40}(?:bag|baggage|luggage)|(?:bag|baggage|luggage).{0,40}lost",
         ("Baggage Services", "Lost Baggage", "")),
        (r"bag|baggage|luggage|suitcase",
         ("Baggage Services", "Damage Baggage", "")),
        (r"cancel(?:led|ed|lation)?",
         ("Flights", "Flight Cancellation", "Flight Cancellation")),
        (r"\b(?:delay|delayed|late)\b",
         ("Flights", "Flight Delay", "Flight Delay")),
        (r"flight",
         ("Flights", "", "")),
    )
    return next((categories for pattern, categories in mappings
                 if re.search(pattern, text)),
                ("Customer Service", "", ""))


def _gaca_live_select_options(page, select_id: str) -> list[str]:
    try:
        return list(page.evaluate(
            """(selectId) => {
                const el = document.getElementById(selectId);
                if (!el) return [];
                return Array.from(el.options || [])
                    .map(o => (o.text || '').trim())
                    .filter(t => t && t.toLowerCase() !== 'select');
            }""",
            select_id,
        ) or [])
    except Exception:
        return []


def _wait_gaca_live_select_options(
        page, select_id: str, timeout_ms: int = 7000) -> list[str]:
    """Wait for GACA's dependent category API to populate a select."""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while time.monotonic() < deadline:
        options = _gaca_live_select_options(page, select_id)
        if options:
            return options
        page.wait_for_timeout(150)
    return _gaca_live_select_options(page, select_id)


def _pick_gaca_option(options: list[str], preferred: str,
                      fallback_patterns: list[str] | None = None) -> str:
    if preferred:
        exact = next((opt for opt in options
                      if opt.casefold() == preferred.casefold()), "")
        if exact:
            return exact
        soft = next((opt for opt in options
                     if preferred.casefold() in opt.casefold()), "")
        if soft:
            return soft
    for pattern in fallback_patterns or []:
        match = next((opt for opt in options
                      if re.search(pattern, opt, re.I)), "")
        if match:
            return match
    return options[0] if options else preferred


def _select_gaca_category_value(
        page,
        select_id: str,
        preferred: str,
        fallback_patterns: list[str] | None = None) -> str:
    """Set and verify one exact native select in GACA's category tree."""
    options = _gaca_live_select_options(page, select_id)
    if not options:
        return ""
    selected = _pick_gaca_option(
        options, preferred, fallback_patterns=fallback_patterns)
    control = page.locator(f"select#{select_id}")
    if control.count() != 1 or not _select_native_option(
            control.first, [rf"^{re.escape(selected)}$"]):
        raise RuntimeError(
            f"GACA category control {select_id!r} could not be set "
            f"to {selected!r}.")
    page.wait_for_timeout(700)
    try:
        selected_text = re.sub(
            r"\s+", " ",
            control.first.locator("option:checked").inner_text()).strip()
        value = str(control.first.input_value() or "").strip()
    except Exception:
        selected_text = ""
        value = ""
    if not value or selected_text.casefold() != selected.casefold():
        raise RuntimeError(
            f"GACA category control {select_id!r} did not retain "
            f"{selected!r}.")
    return selected


def _trigger_gaca_category_change(page, select_id: str) -> None:
    """Trigger both native and jQuery handlers used by GACA's category tree."""
    try:
        page.locator(f"select#{select_id}").evaluate(
            """el => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                if (window.jQuery) window.jQuery(el).trigger('change');
            }""")
    except Exception:
        pass


def _wait_gaca_dependent_options(
        page,
        *,
        parent_id: str,
        parent_value: str,
        child_id: str,
        timeout_ms: int = 7000) -> list[str]:
    """Retry GACA's occasionally missed dependent-category request safely."""
    for attempt in range(3):
        _trigger_gaca_category_change(page, parent_id)
        options = _wait_gaca_live_select_options(
            page, child_id, timeout_ms=timeout_ms)
        if options:
            return options
        if attempt >= 2:
            break
        control = page.locator(f"select#{parent_id}")
        try:
            control.first.select_option(index=0)
            page.wait_for_timeout(350)
        except Exception:
            pass
        _select_gaca_category_value(page, parent_id, parent_value)
        page.wait_for_timeout(650)
    return _gaca_live_select_options(page, child_id)


def _select_gaca_category_tree(page, payload: dict) -> tuple[str, str, str]:
    """Set main/sub/sub-sub using live options so required levels are filled."""
    # The controls become visible before the inline DOMContentLoaded handler
    # that populates the dependent selects has necessarily been attached.
    # Selecting too early leaves SubCategory at "Select" forever.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    # This page exposes the native selects while readyState is "interactive".
    # Its category data is already embedded, but a late DOMContentLoaded
    # callback has not yet attached the change handlers because unrelated
    # third-party accessibility scripts can keep the real event blocked for
    # tens of seconds. Trigger that already-registered page callback as soon
    # as its inline script exists; otherwise GACA's short-lived wizard state
    # can expire before the category POST.
    try:
        page.wait_for_function(
            """() => Array.from(document.scripts).some(
                s => !s.src && /categoryData\\s*=/.test(s.textContent || '')
                    && /categorySelect/.test(s.textContent || ''))""",
            timeout=30_000,
        )
        if page.evaluate("document.readyState !== 'complete'"):
            page.evaluate(
                "document.dispatchEvent(new Event('DOMContentLoaded'))")
        page.wait_for_timeout(150)
    except Exception:
        pass
    main, sub, detail = _gaca_categories(payload)
    exact_main = _select_gaca_category_value(
        page, "categorySelect", main,
        [r"baggage", r"on board", r"flight", r"customer"])
    if exact_main:
        main = exact_main
    elif not _select_gaca_dropdown(page, r"^main category", main):
        raise RuntimeError(f"GACA Main Category could not be set ({main!r}).")
    else:
        page.wait_for_timeout(700)
    sub_options = _wait_gaca_dependent_options(
        page,
        parent_id="categorySelect",
        parent_value=main,
        child_id="subCategorySelect",
    )
    category_text = " ".join([
        str(payload.get("incident") or ""),
        str((payload.get("ai_analysis") or {}).get("category") or ""),
    ]).casefold()
    sub_fallbacks = [
        r"cancel" if "cancel" in category_text else r"(?!x)x",
        r"delay" if re.search(r"\b(?:delay|delayed|late)\b",
                              category_text) else r"(?!x)x",
        r"damage", r"lost", r"defect", r"screen", r"entertainment",
    ]
    if sub_options:
        sub = _select_gaca_category_value(
            page, "subCategorySelect", sub,
            sub_fallbacks)
    elif sub:
        if os.environ.get(
                "FLIGHTBOT_GACA_DEBUG_NETWORK", "").strip().casefold() in {
                    "1", "true", "yes", "on"}:
            try:
                state = page.evaluate("""() => {
                    const el = document.getElementById('categorySelect');
                    const jq = window.jQuery && el
                        ? window.jQuery._data(el, 'events') : null;
                    return {
                        readyState: document.readyState,
                        category: el ? el.outerHTML : '',
                        onchange: el && el.onchange
                            ? String(el.onchange).slice(0, 800) : '',
                        jqueryEvents: jq ? Object.keys(jq) : [],
                        scripts: Array.from(document.scripts)
                            .map(s => s.src || '[inline]')
                            .slice(-20),
                        categoryScripts: Array.from(document.scripts)
                            .filter(s => !s.src && /categorySelect|subCategorySelect/i
                                .test(s.textContent || ''))
                            .map(s => (s.textContent || '').trim().slice(0, 6000))
                    };
                }""")
                logger.warning(
                    "GACA category handler diagnostics: %s",
                    json.dumps(state, ensure_ascii=False, default=str))
            except Exception:
                logger.warning(
                    "Could not inspect GACA category handler state",
                    exc_info=True)
        raise RuntimeError(
            "GACA did not load any SubCategory options after selecting "
            f"{main!r}.")
    detail_options = _wait_gaca_dependent_options(
        page,
        parent_id="subCategorySelect",
        parent_value=sub,
        child_id="subSubCategorySelect",
    )
    detail_fallbacks = [
        r"cancel" if "cancel" in category_text else r"(?!x)x",
        r"delay" if re.search(r"\b(?:delay|delayed|late)\b",
                              category_text) else r"(?!x)x",
        r"damage", r"lost", r"defect", r"screen", r"in-?\s*flight",
    ]
    if detail_options:
        detail = _select_gaca_category_value(
            page, "subSubCategorySelect", detail,
            detail_fallbacks)
    elif detail:
        raise RuntimeError(
            "GACA did not load any final category options after selecting "
            f"{sub!r}.")
    return main, sub, detail


def _select_gaca_dropdown(page, label_pattern: str, value: str) -> bool:
    """Select a GACA dropdown via native select or Selectize."""
    value = str(value or "").strip()
    if not value:
        return False
    choices = [rf"^{re.escape(value)}$", re.escape(value), value]
    if _select(page, [label_pattern], choices):
        return True
    return _selectize_by_label(page, label_pattern, value, choices)


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


def _selectize_value_set(page, control_id: str, choices: list[str]) -> bool:
    """Set a Selectize control via its JS API when present."""
    if not control_id or not choices:
        return False
    try:
        return bool(page.evaluate(
            """({controlId, patterns}) => {
                const el = document.getElementById(controlId);
                if (!el || !el.selectize) return false;
                const entries = Object.entries(el.selectize.options || {});
                const match = entries.find(([, opt]) => {
                    const text = String((opt && (opt.text || opt.label || opt.value)) || '');
                    return patterns.some(pattern => {
                        try { return new RegExp(pattern, 'i').test(text); }
                        catch (err) { return text.toLowerCase().includes(String(pattern).toLowerCase()); }
                    });
                });
                if (!match) return false;
                el.selectize.setValue(match[0], true);
                return !!(el.value && String(el.value).trim());
            }""",
            {"controlId": control_id, "patterns": list(choices)},
        ))
    except Exception:
        return False


def _selectize_by_label(page, label_pattern: str, query: str,
                        choices: list[str]) -> bool:
    """Choose an item from GACA's Selectize-backed hidden selects."""
    choices = [choice for choice in choices if choice]
    query = str(query or "").strip()
    labels = page.locator("label").all()
    for label in labels:
        try:
            if not label.is_visible() or not re.search(
                    label_pattern, label.inner_text(), re.I):
                continue
            control_id = str(label.get_attribute("for") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", control_id):
                continue
            select = page.locator(f"select#{control_id}")
            if select.count() != 1:
                select = page.locator(f"#{control_id}")
                if select.count() != 1:
                    continue
            # Prefer the Selectize API — typing "+966" does not reliably filter.
            if _selectize_value_set(page, control_id, choices):
                return True
            # Some deployments leave the native select visible with full options.
            try:
                shown = select.is_visible()
            except Exception:
                shown = False
            if shown:
                match = _match_select_option(select.first, choices)
                if match and _select_native_option(select.first, choices):
                    return True
            input_control = page.locator(
                f"#{control_id} + .selectize-control input")
            candidate = _wait_for_any_visible(page, input_control, 1500)
            if candidate is None:
                continue
            # Prefer alphabetic search text; dial codes alone often fail to match.
            type_query = query
            if re.fullmatch(r"\+?\d{1,4}", type_query or ""):
                type_query = "Saudi" if "966" in type_query else type_query.lstrip("+")
            if not type_query:
                type_query = "Saudi" if any(
                    re.search(r"966|Saudi", choice, re.I) for choice in choices
                ) else (choices[0] if choices else "")
            candidate.click(force=True)
            candidate.fill("")
            candidate.type(str(type_query), delay=35)
            page.wait_for_timeout(850)
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
                    page.wait_for_timeout(400)
                    try:
                        value = str(select.first.input_value() or "").strip()
                    except Exception:
                        value = ""
                    if value:
                        return True
                    if _selectize_value_set(page, control_id, choices):
                        return True
            # Final API retry after typing opened the full option map.
            if _selectize_value_set(page, control_id, choices):
                return True
        except Exception:
            continue
    return False


_GACA_CITY_NAMES = {
    "AHB": "Abha", "BAH": "Manama", "CAI": "Cairo",
    "DMM": "Dammam", "DXB": "Dubai", "JED": "Jeddah",
    "LHR": "London", "MED": "Madinah", "NUM": "Neom",
    "RUH": "Riyadh",
}


def _is_gaca_login_page(page) -> bool:
    """True when MyEservices is asking for username/password or Nafath."""
    try:
        url = (page.url or "").casefold()
    except Exception:
        url = ""
    if "login" in url or "signin" in url or "nafath" in url:
        return True
    if _visible(page.locator("input[type='password']")):
        return True
    body = _body_text(page)
    if re.search(r"\bNafath\b|\bنفاذ\b", body, re.I) and re.search(
            r"sign\s*in|username|password|login", body, re.I):
        return True
    return False


def _gaca_nafath_failure(page) -> str:
    """Return GACA's current Nafath error text, if the portal rendered one."""
    body = re.sub(r"\s+", " ", _body_text(page)).strip()
    match = re.search(
        r"(Authentication using Nafath failed|Nafath authentication failed|"
        r"تعذر[^.،\n]{0,80}نفاذ|فشل[^.،\n]{0,80}نفاذ)",
        body, re.I)
    return match.group(1).strip() if match else ""


def _gaca_login_abort_message(page) -> str:
    failure = _gaca_nafath_failure(page)
    if failure:
        return (
            "GACA's Nafath authentication endpoint rejected the login "
            f"before the complaint form opened ({failure}). Nothing was "
            "submitted; the durable job will retry after an outage cooldown."
        )
    return (
        "GACA MyEservices sign-in was not completed; escalation aborted "
        "safely before the complaint form."
    )


def _dismiss_gaca_cookie_banner(page) -> bool:
    """Remove GACA's cookie overlay before Nafath input or screenshots."""
    for selector in (
            "#rejectCookies",
            "#acceptCookies",
            "#closeCookiePopup",
            "[data-cookie-action='reject']",
            "[data-cookie-action='accept']"):
        try:
            control = page.locator(selector)
            if not control.count():
                continue
            control = control.first
            if not control.is_visible():
                continue
            control.click(force=True, timeout=3000)
            page.wait_for_timeout(250)
            return True
        except Exception:
            continue
    return False


def _click_gaca_nafath_submit(page) -> bool:
    """Click the Nafath submit button, not the identically named tab."""
    _dismiss_gaca_cookie_banner(page)
    try:
        buttons = page.locator("button[type='submit']")
        for index in range(buttons.count() - 1, -1, -1):
            button = buttons.nth(index)
            if not button.is_visible() or not button.is_enabled():
                continue
            label = re.sub(r"\s+", " ", button.inner_text()).strip()
            if re.fullmatch(r"Nafath|نفاذ", label, re.I):
                button.click(force=True, no_wait_after=True)
                return True
    except Exception:
        pass
    return _click(
        page,
        [r"^Nafath$", r"^Login$", r"^Sign\s*in$",
         r"^تسجيل\s*الدخول$"],
    )


def _start_gaca_nafath(page, payload: dict, update) -> str:
    """Walk GACA's two National-ID screens and request phone approval."""
    if not _is_gaca_login_page(page):
        return "authenticated"
    _dismiss_gaca_cookie_banner(page)
    try:
        url = str(page.url or "").casefold()
    except Exception:
        url = ""

    # The current GACA login first exposes a Nafath tab and then loads a
    # dedicated /login/nafath page that asks for the National ID again.
    if "/login" in url and "/nafath" not in url:
        if not _click(page, [r"^Nafath$", r"^نفاذ$"]):
            return "manual"
        update("verification", "Opening GACA's Nafath sign-in…")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            try:
                url = str(page.url or "").casefold()
            except Exception:
                url = ""
            if "/nafath" in url or not _is_gaca_login_page(page):
                break
        if not _is_gaca_login_page(page):
            return "authenticated"
        _dismiss_gaca_cookie_banner(page)

    national_id = str(payload.get("national_id") or "").strip()
    if not national_id:
        return "manual"
    try:
        url = str(page.url or "").casefold()
    except Exception:
        url = ""
    body = _body_text(page)
    nafath_page = (
        "/nafath" in url
        or bool(re.search(r"National\s*ID|رقم\s*(?:الهوية|الإقامة)", body, re.I))
    )
    if not nafath_page:
        return "manual"

    filled = _fill(
        page,
        [r"national\s*(?:id|identity)", r"(?:id|identity)\s*number",
         r"رقم\s*(?:الهوية|الإقامة)"],
        national_id,
        required=True)
    if not filled:
        # The accessible label sometimes disappears while this Angular page
        # hydrates. On the dedicated Nafath route, one editable text input is
        # still an unambiguous National-ID target.
        try:
            inputs = page.locator(
                "input:not([type=hidden]):not([type=password])")
            editable = [
                inputs.nth(index) for index in range(inputs.count())
                if inputs.nth(index).is_visible()
                and inputs.nth(index).is_editable()
            ]
        except Exception:
            editable = []
        if len(editable) == 1:
            editable[0].fill(national_id)
            filled = True
    if not filled:
        return "manual"

    # The current portal labels the National-ID submit button "Nafath".
    # Older deployments used "Login" or "Sign in".
    clicked = _click_gaca_nafath_submit(page)
    if not clicked:
        return "manual"
    page.wait_for_timeout(1200)
    _dismiss_gaca_cookie_banner(page)
    approval_text = re.sub(r"\s+", " ", _body_text(page)).strip()
    approval_match = re.search(
        r"(?:code below|verification code|approval (?:code|number))"
        r"[^0-9]{0,80}(\d{2})\b",
        approval_text,
        re.I,
    )
    approval_suffix = (
        f" Approve number {approval_match.group(1)} in the Nafath app."
        if approval_match else ""
    )
    update(
        "verification",
        "GACA accepted the National ID request. Waiting for Nafath approval."
        + approval_suffix,
        _page_screenshot(page))

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if not _is_gaca_login_page(page):
            return "authenticated"
        failure = _gaca_nafath_failure(page)
        if failure:
            update(
                "verification",
                f"GACA rejected this Nafath login request ({failure}). "
                "Nothing was submitted; the job will retry safely.",
                _page_screenshot(page))
            return "failed"
    return "approval"


def _wait_for_gaca_login(page, update, payload: dict | None = None,
                         timeout_seconds: int = 900) -> bool:
    """Start Nafath automatically, then wait only for phone approval."""
    if not _is_gaca_login_page(page):
        return True
    payload = dict(payload or {})
    nafath_state = _start_gaca_nafath(page, payload, update)
    if nafath_state == "authenticated":
        return True
    if nafath_state == "failed":
        return False
    if nafath_state == "approval":
        prompt = (
            "GACA sent a Nafath approval request. Approve it on your phone, "
            "then reply DONE here.")
    else:
        prompt = (
            "GACA MyEservices requires sign-in. The saved National ID could "
            "not be applied safely on this page; finish Nafath/password "
            "sign-in, then reply DONE here.")
    update(
        "verification",
        prompt,
        _page_screenshot(page))
    if _VERIFICATION_HANDLER:
        response = _ask_verification(
            "login",
            f"{prompt} Reply CANCEL to abort.",
            page,
            choices=["DONE", "CANCEL"])
        if response and str(response).strip().casefold() in {
                "cancel", "cancelled", "canceled"}:
            return False
    deadline = time.monotonic() + max(60, int(timeout_seconds))
    reminded = False
    while time.monotonic() < deadline:
        if not _is_gaca_login_page(page):
            update(
                "filling",
                "GACA sign-in completed. Continuing with the form…",
                _page_screenshot(page))
            return True
        failure = _gaca_nafath_failure(page)
        if failure:
            update(
                "verification",
                f"GACA rejected this Nafath login request ({failure}). "
                "Nothing was submitted; the job will retry safely.",
                _page_screenshot(page))
            return False
        page.wait_for_timeout(2500)
        if (not reminded and _VERIFICATION_HANDLER
                and time.monotonic() + 180 > deadline):
            reminded = True
            response = _ask_verification(
                "login",
                "Still on the GACA login page. Finish Nafath/password sign-in, "
                "then reply DONE.",
                page,
                choices=["DONE", "CANCEL"])
            if response and str(response).strip().casefold() in {
                    "cancel", "cancelled", "canceled"}:
                return False
            deadline = time.monotonic() + 300
    update(
        "verification",
        "GACA sign-in was not completed before the timeout. The escalation was "
        "not submitted.",
        _page_screenshot(page))
    return False


def _gaca_gender_choices(payload: dict) -> list[str]:
    """Male/Female patterns derived from the passenger profile title/gender."""
    gender = str(payload.get("gender") or "").strip()
    title = str(payload.get("title") or "").strip().casefold()
    if not gender:
        if title in {"mr", "mr.", "mister"}:
            gender = "Male"
        elif title in {"mrs", "mrs.", "ms", "ms.", "miss", "miss."}:
            gender = "Female"
    wanted = gender.casefold()
    if wanted.startswith("f") or wanted in {"أنثى", "انثى", "female"}:
        return [r"^Female$", r"^FEMALE$", r"^أنثى$", r"^انثى$", r"^F$"]
    return [r"^Male$", r"^MALE$", r"^ذكر$", r"^M$"]


def _select_gaca_gender(page, payload: dict) -> bool:
    """Set GACA Gender on select[name=gender] (values are MALE/FEMALE)."""
    choices = _gaca_gender_choices(payload)
    female = any(re.search(r"Female|FEMALE|أنثى|انثى|^F\$", c, re.I)
                 for c in choices)
    wanted_value = "FEMALE" if female else "MALE"
    wanted_label = "Female" if female else "Male"
    select = page.locator("select[name='gender'], select#gender")
    try:
        page.wait_for_function(
            """wanted => Array.from(
                document.querySelectorAll(
                    "select[name='gender'] option, select#gender option")
            ).some(option => String(option.value || '').toUpperCase() === wanted)""",
            wanted_value,
            timeout=7000,
        )
    except Exception:
        pass
    if select.count():
        target = select.first
        for _retry in range(3):
            for attempt in (
                    {"value": wanted_value},
                    {"label": wanted_label},
            ):
                try:
                    # GACA briefly overlays/rebuilds this native select after
                    # Step 2 loads. Selecting by value is still valid while
                    # the overlay is present, so do not fail a durable job
                    # merely because Playwright's actionability check races it.
                    target.select_option(**attempt, force=True)
                    page.wait_for_timeout(250)
                    if str(
                            target.input_value() or ""
                    ).strip().upper() == wanted_value:
                        try:
                            target.evaluate(
                                """el => {
                                    el.dispatchEvent(new Event(
                                        'input', {bubbles: true}));
                                    el.dispatchEvent(new Event(
                                        'change', {bubbles: true}));
                                }""")
                        except Exception:
                            pass
                        return True
                except Exception:
                    continue
            try:
                target.evaluate(
                    """(el, wanted) => {
                        const option = Array.from(el.options).find(
                            item => String(item.value || '').toUpperCase()
                                === wanted);
                        if (!option) return false;
                        el.value = option.value;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""",
                    wanted_value,
                )
                page.wait_for_timeout(300)
                if str(
                        target.input_value() or ""
                ).strip().upper() == wanted_value:
                    return True
            except Exception:
                pass
    if _select(page, ["gender", r"^sex$"], choices):
        try:
            if select.count() and str(
                    select.first.input_value() or "").strip().upper() == wanted_value:
                return True
        except Exception:
            return True
    if _selectize_by_label(page, r"gender|^sex$", wanted_label, choices):
        return True
    return False


def _gaca_step2_invalid_summary(page) -> str:
    try:
        rows = page.evaluate("""() => Array.from(
            document.querySelectorAll('select, input, textarea')
        ).filter(el => {
            if (!el.required) return false;
            // Ignore off-screen feedback widgets that share name=gender radios.
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const hidden = style.display === 'none' || style.visibility === 'hidden'
                || (rect.width === 0 && rect.height === 0 && el.tagName !== 'SELECT');
            if (hidden && el.tagName !== 'SELECT') return false;
            if (el.closest && el.closest('#feedbackForm, .feedback-form, form[id*="feedback" i]'))
                return false;
            return !el.checkValidity();
        }).map(el => `${el.name || el.id || el.tagName}: ${el.validationMessage || 'invalid'}`))""")
    except Exception:
        rows = []
    return "; ".join(rows[:6])


def _gaca_rate_limit_abort(
        payload: dict, page=None, update=None) -> GacaIdentityRateLimitError:
    """Pause the affected passenger without rotating a healthy network."""
    if page is not None and update is not None:
        update(
            "verification",
            "GACA returned its technical “too many submission attempts” "
            "page before final Submit. Nothing was submitted; the current "
            "page is captured. Only this passenger will wait 24 hours; "
            "other passengers can continue.",
            _page_screenshot(page),
        )
    try:
        rejection_count = int(
            payload.get("_gaca_technical_rejections") or 0) + 1
    except (TypeError, ValueError):
        rejection_count = 1
    payload["_gaca_technical_rejections"] = min(rejection_count, 100)
    payload["_gaca_restart_normal_browser"] = False
    payload["_gaca_waf_rotated"] = False
    return GacaIdentityRateLimitError(
        "GACA reported too many submission attempts for this passenger "
        "identity before Submit. FlightDeck will retry only this passenger "
        "after 24 hours; other passengers remain eligible.")


def _gaca_waf_abort(
        payload: dict, page=None, update=None, *,
        after_submit: bool = False) -> GacaWafBlockedError:
    """Rotate the proxy and force a clean GACA browser after a WAF block."""
    if page is not None and update is not None:
        stage_message = (
            "GACA's WAF replaced the confirmation page, so acceptance is "
            "not verified."
            if after_submit else
            "GACA's WAF blocked the browser before final Submit. Nothing "
            "was submitted."
        )
        update(
            "verification",
            f"{stage_message} The blocked page is saved before FlightDeck "
            "changes IP and restarts a clean GACA browser.",
            _page_screenshot(page),
        )
    try:
        rejection_count = int(payload.get("_gaca_waf_rejections") or 0) + 1
    except (TypeError, ValueError):
        rejection_count = 1
    payload["_gaca_waf_rejections"] = min(rejection_count, 100)
    rotated = gaca_normal_browser.rotate_proxy_session()
    payload["_gaca_restart_normal_browser"] = rotated
    payload["_gaca_waf_rotated"] = rotated
    if rotated:
        return GacaWafBlockedError(
            "GACA's WAF rejected the browser"
            + (
                " after Submit without verified acceptance. "
                if after_submit else " before Submit. "
            )
            + "FlightDeck "
            "rotated the GACA residential proxy and will relaunch normal "
            "Chrome with clean GACA cookies and storage before retrying.")
    return GacaWafBlockedError(
        "GACA's WAF rejected the browser"
        + (
            " after Submit without verified acceptance. "
            if after_submit else " before Submit. "
        )
        + "Automatic proxy "
        "rotation is not configured, so FlightDeck closed this attempt and "
        "will retry after a longer cooldown.")


def _gaca_dwell(page, started: float, env_name: str, default: int) -> None:
    """Keep each public wizard page open long enough to avoid burst traffic."""
    raw = os.environ.get(env_name, str(default)).strip()
    try:
        seconds = max(0, min(int(raw), 120))
    except (TypeError, ValueError):
        seconds = default
    remaining = seconds - (time.monotonic() - started)
    if remaining > 0:
        page.wait_for_timeout(round(remaining * 1000))


def _prepare_gaca(page, payload: dict, update):
    update("opening", "Opening GACA’s official Airline Complaint service…")
    if not _wait_for_gaca_login(page, update, payload):
        raise RuntimeError(_gaca_login_abort_message(page))
    if _click(page, [r"^Apply Now$", "Apply For Service", "تقديم الآن"]):
        _wait_for_any_visible(
            page, page.get_by_role("button", name=re.compile(r"^Next$", re.I)),
            7000)
    if _is_gaca_login_page(page) or _visible(page.locator("input[type='password']")):
        if not _wait_for_gaca_login(page, update, payload):
            raise RuntimeError(_gaca_login_abort_message(page))
        page.wait_for_timeout(1500)
        if _click(page, [r"^Apply Now$", "Apply For Service", "تقديم الآن"]):
            _wait_for_any_visible(
                page,
                page.get_by_role("button", name=re.compile(r"^Next$", re.I)),
                7000)
    step1_started = time.monotonic()
    update(
        "filling",
        "GACA step 1 of 4 is open: reviewing the escalation requirements…",
        _page_screenshot(page),
    )
    _gaca_dwell(
        page, step1_started, "FLIGHTBOT_GACA_STEP1_DWELL_SECONDS", 3)
    if _click(page, [r"^Next$"]):
        _wait_for_any_visible(
            page, page.get_by_label(re.compile("first name", re.I)), 7000)

    step2_started = time.monotonic()
    update("filling", "GACA step 2 of 4: filling personal information…")
    _fill(page, ["first name"], payload["first_name"])
    middle = str(payload.get("middle_name") or "").strip()
    if not middle:
        raise RuntimeError(
            "GACA requires a real middle name; set user.middle_name in config.")
    _fill(page, ["middle name"], middle)
    _fill(page, ["family name", "last name"], payload["last_name"])
    _fill(page, [r"^email", "email"], payload["email"])
    _fill(page, [r"^mobile", "mobile"], _gaca_mobile(payload))
    _fill(page, ["national id", "passport number"], payload["national_id"])
    if not _select_gaca_gender(page, payload):
        raise RuntimeError(
            "GACA Gender could not be set from the passenger profile "
            f"(title={payload.get('title')!r}, gender={payload.get('gender')!r}).")
    country_code = str(payload.get("country_code") or "").strip()
    country_choices = [re.escape(country_code)] if country_code else []
    if re.sub(r"\D", "", country_code) == "966":
        country_choices += [
            r"Saudi Arabia\s*\|\s*\+966", r"Saudi Arabia.*\+966", r"\+966"]
    country_query = (
        "Saudi Arabia" if re.sub(r"\D", "", country_code) == "966"
        else (country_code.lstrip("+") or "Saudi"))
    if not _selectize_by_label(
            page, r"country\s*code", country_query,
            country_choices or [r"Saudi Arabia"]):
        raise RuntimeError(
            "GACA Country Code could not be set "
            f"(country_code={country_code!r}).")
    # Confirm the hidden select actually holds a value before clicking Next.
    country_value = "ok"
    try:
        country_el = page.locator("select#countryCode")
        if country_el.count():
            country_value = country_el.input_value()
            if not str(country_value or "").strip():
                raise RuntimeError(
                    "GACA Country Code Selectize did not keep a selected value.")
    except RuntimeError:
        raise
    except Exception:
        pass
    update(
        "reviewing",
        "GACA step 2 of 4 is complete: personal information is filled and "
        "checked before Next.",
        _page_screenshot(page),
    )
    _gaca_dwell(
        page, step2_started, "FLIGHTBOT_GACA_STEP2_DWELL_SECONDS", 20)
    if _click(page, [r"^Next$"]):
        advanced = _wait_for_any_visible(
            page, page.get_by_label(re.compile("main category", re.I)), 7000)
        if advanced is None:
            # The rate-limit banner is rendered shortly after the redirect
            # back to Step 2. Detect it before issuing a second POST; repeating
            # the same personal-information request only prolongs the block.
            try:
                page.wait_for_function(
                    """() => /too many submission attempts/i.test(
                        document.body ? document.body.innerText : '')""",
                    timeout=7000,
                )
            except Exception:
                pass
            if re.search(
                    r"too many submission attempts",
                    _body_text(page), re.I):
                raise _gaca_rate_limit_abort(payload, page, update)
            last_post_status = int(getattr(
                page, "_flightdeck_gaca_last_post_status", 0) or 0)
            last_post_location = str(getattr(
                page, "_flightdeck_gaca_last_post_location", "") or "")
            last_post_path = urlparse(last_post_location).path.casefold()
            if last_post_status == 403:
                raise _gaca_waf_abort(payload, page, update)
            if last_post_path.endswith("/step2"):
                detail = (
                    _gaca_step2_invalid_summary(page)
                    or "portal redirected the identity back to step 2"
                )
                raise RuntimeError(
                    "GACA rejected the personal-information step before "
                    f"Submit ({detail}). The same POST was not repeated.")
            if last_post_path.endswith("/step3"):
                advanced = _wait_for_any_visible(
                    page,
                    page.get_by_label(re.compile("main category", re.I)),
                    10000,
                )
                if advanced is None:
                    raise RuntimeError(
                        "GACA accepted personal information and redirected "
                        "to step 3, but the category controls did not finish "
                        "loading. The identity POST was not repeated.")
            else:
                # If no POST response was observed, the first click may have
                # been stopped by client-side widget state. Repair it once.
                _select_gaca_gender(page, payload)
                _selectize_by_label(
                    page, r"country\s*code", country_query,
                    country_choices or [r"Saudi Arabia"])
                if _click(page, [r"^Next$"]):
                    advanced = _wait_for_any_visible(
                        page,
                        page.get_by_label(
                            re.compile("main category", re.I)),
                        7000,
                    )
                if advanced is None:
                    try:
                        page.wait_for_function(
                            """() => /too many submission attempts/i.test(
                                document.body ? document.body.innerText : '')""",
                            timeout=7000,
                        )
                    except Exception:
                        pass
                    last_post_status = int(getattr(
                        page,
                        "_flightdeck_gaca_last_post_status",
                        0,
                    ) or 0)
                    if last_post_status == 403:
                        raise _gaca_waf_abort(payload, page, update)
                    if re.search(
                            r"too many submission attempts",
                            _body_text(page), re.I):
                        raise _gaca_rate_limit_abort(payload, page, update)
                    detail = (
                        _gaca_step2_invalid_summary(page)
                        or "unknown required field"
                    )
                    raise RuntimeError(
                        "GACA did not advance past personal information "
                        f"({detail}).")

    step3_started = time.monotonic()
    update("filling", "GACA step 3 of 4: selecting the complaint category…")
    payload["_gaca_technical_rejections"] = 0
    payload["_gaca_waf_rejections"] = 0
    main, sub, detail = _select_gaca_category_tree(page, payload)
    payload["selected_complaint_category"] = " › ".join(
        value for value in (main, sub, detail) if value)
    update(
        "reviewing",
        "GACA step 3 of 4 is complete: the complaint category is selected "
        "and checked before Next.",
        _page_screenshot(page),
    )
    if os.environ.get(
            "FLIGHTBOT_GACA_DEBUG_NETWORK", "").strip().casefold() in {
                "1", "true", "yes", "on"}:
        try:
            csrf_state = page.evaluate("""() => {
                const category = document.getElementById('categorySelect');
                const form = category ? category.closest('form') : null;
                const inputs = form
                    ? Array.from(form.querySelectorAll('input[name="_csrf"]'))
                    : [];
                const values = inputs.map(input => input.value || '');
                const meta = document.querySelector(
                    'meta[name="_csrf"], meta[name="csrf-token"]');
                const metaValue = meta ? meta.content || '' : '';
                const visibleCookies = document.cookie.split(';')
                    .map(value => value.trim())
                    .filter(Boolean);
                return {
                    formAction: form ? form.action : '',
                    formMethod: form ? form.method : '',
                    tokenCount: values.length,
                    tokenLengths: values.map(value => value.length),
                    tokensEqual: values.length > 1
                        ? values.every(value => value === values[0]) : true,
                    matchesMeta: values.map(
                        value => Boolean(metaValue) && value === metaValue),
                    cookieNames: visibleCookies.map(
                        value => value.split('=', 1)[0]),
                    tokenMatchesVisibleCookie: values.map(value =>
                        visibleCookies.some(cookie =>
                            decodeURIComponent(
                                cookie.slice(cookie.indexOf('=') + 1)) === value)),
                    tokenParents: inputs.map(input => ({
                        parentTag: input.parentElement
                            ? input.parentElement.tagName : '',
                        parentId: input.parentElement
                            ? input.parentElement.id || '' : '',
                        parentClass: input.parentElement
                            ? input.parentElement.className || '' : ''
                    })),
                    categoryScripts: Array.from(document.scripts)
                        .filter(s => !s.src && /categorySelect|subCategorySelect/i
                            .test(s.textContent || ''))
                        .map(s => (s.textContent || '').trim().slice(0, 8000))
                };
            }""")
            logger.warning(
                "GACA category CSRF diagnostics: %s",
                json.dumps(csrf_state, ensure_ascii=False, default=str))
        except Exception:
            logger.warning(
                "Could not inspect GACA category CSRF state",
                exc_info=True)
        if os.environ.get(
                "FLIGHTBOT_GACA_DEBUG_STOP_BEFORE_STEP3_POST",
                "").strip().casefold() in {"1", "true", "yes", "on"}:
            update(
                "reviewing",
                "Live debug paused before GACA's category POST; nothing was "
                "submitted.",
                _page_screenshot(page),
            )
            raise RuntimeError(
                "GACA live debug stopped before the category POST.")
    _gaca_dwell(
        page, step3_started, "FLIGHTBOT_GACA_STEP3_DWELL_SECONDS", 8)
    if _click(page, [r"^Next$"]):
        final_step = page.locator(
            "input#flightDate, input[name='flightDate'], "
            "input[id*='flightDate' i], input[name*='flightDate' i]")
        advanced = _wait_for_any_visible(page, final_step, 7000)
        if advanced is None:
            advanced = _wait_for_any_visible(
                page,
                page.get_by_label(re.compile(
                    r"flight\s*date|date\s*of\s*(?:the\s*)?flight", re.I)),
                1500)
        if advanced is None and re.search(
                r"/complaint-airline/step4(?:\?|$)", str(page.url), re.I):
            # The current Step 4 initially renders only its From/To Selectize
            # controls. The remaining flight fields are added after both route
            # values are selected.
            route_controls = page.locator(
                "form#complaintForm select#fromCity, "
                "form#complaintForm select#toCity")
            if route_controls.count() >= 2:
                advanced = route_controls.first
        if advanced is None:
            try:
                category_state = page.evaluate("""() => [
                    'categorySelect',
                    'subCategorySelect',
                    'subSubCategorySelect'
                ].map(id => {
                    const el = document.getElementById(id);
                    if (!el) return `${id}=missing`;
                    const selected = el.options && el.selectedIndex >= 0
                        ? el.options[el.selectedIndex].text : '';
                    return `${id}=${el.value || ''} (${selected || ''})`;
                }).join('; ')""")
            except Exception:
                category_state = "unavailable"
            update(
                "filling",
                "GACA did not reveal the final complaint-details page after "
                f"category selection ({category_state}). Nothing was submitted.",
                _page_screenshot(page))
            raise RuntimeError(
                "GACA did not advance past the category step "
                f"({_gaca_step2_invalid_summary(page) or 'category invalid'}; "
                f"{category_state}; URL={page.url}).")

    step4_started = time.monotonic()
    update("filling", "GACA step 4 of 4: filling flight and complaint details…")
    origin = str(payload.get("origin") or "").strip().upper()
    destination = str(payload.get("destination") or "").strip().upper()
    if origin:
        if not _selectize_by_label(
                page, r"flight\s*from", origin,
                [rf"\b{re.escape(origin)}\b",
                 re.escape(_GACA_CITY_NAMES.get(origin, origin))]):
            raise RuntimeError(
                f"GACA departure airport could not be set ({origin!r}).")
    if destination:
        if not _selectize_by_label(
                page, r"flight\s*to", destination,
                [rf"\b{re.escape(destination)}\b",
                 re.escape(_GACA_CITY_NAMES.get(destination, destination))]):
            raise RuntimeError(
                f"GACA arrival airport could not be set ({destination!r}).")
    _wait_for_any_visible(
        page,
        page.locator(
            "select#airline, select[name='airline'], select#airlineId, "
            "input#flightDate, input[name='flightDate']"),
        7000,
    )
    airline = _gaca_airline_label(payload)
    if not _select_gaca_dropdown(page, r"^airline$", airline):
        # Broader label match for "Airline *" / localized labels.
        if not _select_gaca_dropdown(page, r"^airline", airline):
            raise RuntimeError(
                f"GACA Airline could not be set ({airline!r}).")
    # Confirm the hidden/native select kept a value — Selectize can look filled
    # while the bound select is empty, which makes Submit reset the form.
    try:
        airline_el = page.locator(
            "select#airline, select[name='airline'], select#airlineId")
        if airline_el.count():
            airline_value = str(airline_el.first.input_value() or "").strip()
            if not airline_value:
                if not _selectize_value_set(
                        page, "airline",
                        [rf"^{re.escape(airline)}$", re.escape(airline)]):
                    raise RuntimeError(
                        f"GACA Airline Selectize did not keep {airline!r}.")
    except RuntimeError:
        raise
    except Exception:
        pass
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
    # Verify the complaint-details textarea kept the full letter (not just the greeting).
    try:
        details = page.locator("textarea#complaintDetails, textarea[name='complaintDetails']")
        if details.count():
            current = str(details.first.input_value() or "")
            wanted = str(payload.get("description") or "")
            if wanted and len(current.strip()) < min(40, len(wanted)):
                details.first.fill(wanted)
                details.first.evaluate(
                    """el => {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }""")
    except Exception:
        pass
    attachments = [str(path) for path in payload.get("attachments") or []
                   if Path(path).is_file()]
    if attachments:
        inputs = page.locator("input[type='file']")
        if inputs.count():
            inputs.first.set_input_files(attachments)
    _wait_for_any_visible(
        page, page.get_by_role("button", name=re.compile(r"^Submit$", re.I)),
        7000)
    update(
        "reviewing",
        "GACA step 4 of 4 is complete: flight details, complaint text, and "
        "attachments are filled and checked before Submit.",
        _page_screenshot(page),
    )
    _gaca_dwell(
        page, step4_started, "FLIGHTBOT_GACA_STEP4_DWELL_SECONDS", 15)


def _prepare_generic(page, payload: dict, update):
    update("filling", "Filling the airline’s official complaint form…")
    _click(page, ["Complaint", "Complaints & Feedback", "Customer Relations",
                  "Submit a request", "Contact us"])
    page.wait_for_timeout(1200)
    _fill_common(page, payload)


def _extract_reference(text: str) -> str:
    patterns = (
        r"(?:(?:complaint|request|case)\s+)?(?:reference|case|complaint|request)\s*(?:number|no\.?|id|#)?\s*(?:is\s*)?[:#-]?\s*([A-Z][A-Z_-]{0,9}\d{4,})",
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


def _extract_gaca_reference_from_url(url: str) -> str:
    """Extract only a public GACA case number, never an internal detailsId."""
    try:
        parsed = urlparse(str(url or ""))
        if not re.search(r"/public/qpe/survey/?$", parsed.path, re.I):
            return ""
        query = parse_qs(parsed.query)
    except Exception:
        return ""
    for key in (
        "reference", "complaintNumber", "complaintNo",
        "caseNumber", "caseNo",
    ):
        value = str((query.get(key) or [""])[0]).strip()
        if re.fullmatch(r"C\d{6,}", value, re.I):
            return value.upper()
    return ""


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
            + suffix,
            retry_safe=True)
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


def _gaca_security_rejected(value: str) -> bool:
    return bool(re.search(
        r"security\s+check\s+failed|invalid\s+(?:re-?captcha|captcha)|"
        r"(?:re-?captcha|captcha).{0,80}(?:invalid|failed|expired)",
        str(value or ""),
        re.I,
    ))


def _capture_gaca_response(response, capture: dict) -> None:
    """Capture GACA's complaint and OTP POSTs before redirects hide them."""
    try:
        if str(response.request.method).upper() != "POST":
            return
        is_complaint = "/complaint-airline/step4" in response.url
        is_verification = "/verification/email" in response.url
        if not (is_complaint or is_verification):
            return
        headers = getattr(response, "headers", {}) or {}
        if callable(headers):
            headers = headers()
        location = ""
        if isinstance(headers, dict):
            location = str(
                headers.get("location") or headers.get("Location") or "")
        prefix = "verification_" if is_verification else ""
        capture.update({
            f"{prefix}seen": True,
            f"{prefix}status": response.status,
            f"{prefix}url": response.url,
            f"{prefix}location": location,
        })
        try:
            capture[f"{prefix}json"] = response.json()
        except Exception:
            capture[f"{prefix}text"] = response.text()[:8000]
    except Exception:
        return


def _gaca_submission_result(
        capture: dict | None,
        payload: dict | None = None) -> PortalResult | None:
    """Interpret only authoritative signals from GACA's complaint POST."""
    if not capture or not capture.get("seen"):
        return None
    status = int(capture.get("status") or 0)
    body = str(capture.get("text") or "")
    if capture.get("json") is not None:
        try:
            body += "\n" + json.dumps(
                capture.get("json"), ensure_ascii=False, default=str)
        except Exception:
            pass
    location = str(capture.get("location") or "")
    combined = f"{body}\n{location}"
    if _gaca_security_rejected(combined):
        return PortalResult(
            "verification_expired",
            "GACA explicitly rejected the security token. The complaint was "
            "not accepted and is safe to retry later with fresh verification.")
    if status >= 400:
        return PortalResult(
            "error",
            f"GACA rejected the complaint POST with HTTP {status}; no "
            "acceptance was recorded.",
            retry_safe=True,
        )
    success = bool(re.search(
        r"successfully submitted|complaint (?:was )?(?:received|submitted)|"
        r"request (?:was )?received|submission[-_/ ]?(?:success|complete)|"
        r"(?:success|confirmation|thank[-_/ ]?you)",
        combined,
        re.I,
    ))
    if not success:
        return None
    reference = _extract_reference(combined)
    structured = {
        str((payload or {}).get(key) or "").strip().lower()
        for key in ("airline_reference", "pnr", "ticket_number")
    }
    if reference and reference.strip().lower() in structured:
        reference = ""
    if reference:
        return PortalResult(
            "submitted",
            "GACA's complaint POST confirmed acceptance.",
            reference,
        )
    return PortalResult(
        "accepted_pending_reference",
        "GACA's complaint POST confirmed acceptance, but the confirmation "
        "page did not expose the regulator reference. FlightDeck will "
        "reconcile it from email or SMS and will not submit a duplicate.")


def _gaca_verification_result(
        capture: dict | None,
        payload: dict | None = None) -> PortalResult | None:
    """Interpret GACA's OTP POST redirect, the authoritative final decision."""
    if not capture or not capture.get("verification_seen"):
        return None
    try:
        status = int(capture.get("verification_status") or 0)
    except (TypeError, ValueError):
        status = 0
    body = str(capture.get("verification_text") or "")
    if capture.get("verification_json") is not None:
        try:
            body += "\n" + json.dumps(
                capture.get("verification_json"),
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            pass
    location = str(capture.get("verification_location") or "")
    combined = f"{body}\n{location}"
    if _gaca_security_rejected(combined):
        return PortalResult(
            "verification_expired",
            "GACA rejected the final email verification security token. The "
            "complaint was not accepted and is safe to retry.",
        )
    if status >= 400:
        return PortalResult(
            "error",
            f"GACA rejected the email verification with HTTP {status}; no "
            "acceptance was recorded.",
            retry_safe=True,
        )
    if re.search(r"/complaint-airline/step4(?:[/?#]|$)", location, re.I):
        return PortalResult(
            "verification_expired",
            "GACA returned the email verification to the final complaint form. "
            "The complaint was not accepted and is safe to retry with a fresh "
            "OTP and security token.",
        )
    success = bool(re.search(
        r"/(?:survey|submission[-_/]?success)(?:[/?#]|$)|"
        r"successfully submitted|complaint (?:was )?(?:received|submitted)|"
        r"request (?:was )?received",
        combined,
        re.I,
    ))
    if not success:
        return None
    reference = _extract_reference(combined)
    structured = {
        str((payload or {}).get(key) or "").strip().lower()
        for key in ("airline_reference", "pnr", "ticket_number")
    }
    if reference and reference.strip().lower() in structured:
        reference = ""
    if reference:
        return PortalResult(
            "submitted",
            "GACA accepted the email verification and returned a regulator "
            "reference.",
            reference,
        )
    return PortalResult(
        "accepted_pending_reference",
        "GACA accepted the email verification and opened its official survey. "
        "FlightDeck will reconcile the regulator reference from SMS or email "
        "and will not submit a duplicate.",
    )


def _gaca_ai_submission_result(page, payload: dict | None,
                               reason: str) -> PortalResult | None:
    """Use Ghala to classify a post-Submit page without inventing acceptance.

    A model judgment can prove that an error/WAF/form page is not a successful
    confirmation and therefore make a retry safe.  Acceptance still requires a
    literal success signal or reference in the page; the model alone may never
    turn an ambiguous screen into a submitted complaint.
    """
    if not _AI_HANDLER or page.is_closed():
        return None
    decision = _ask_ai(
        page,
        payload or {},
        "GACA Submit was clicked once. Decide whether the visible page is a "
        "real accepted-submission confirmation, an explicit failure/error "
        "that is safe to retry, or still waiting. Do not infer acceptance "
        "from a blank page, WAF page, HTTP success alone, or the pre-submit "
        "greeting. " + reason,
    )
    if not isinstance(decision, dict):
        return None
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    state = str(decision.get("state") or "").strip().lower()
    summary = " ".join(str(decision.get("summary") or "").split())[:500]
    text = _body_text(page)
    reference = (
        _extract_reference(text)
        or _extract_reference_from_url(page.url)
        or _extract_gaca_reference_from_url(page.url)
    )
    literal_success = bool(re.search(
        r"successfully submitted|request (?:was )?received|"
        r"complaint (?:was )?received|submission[-_/ ]?(?:success|complete)|"
        r"رقم (?:الطلب|الشكوى)",
        text,
        re.I,
    ))
    if state == "submitted" and confidence >= 0.85 and (
            literal_success or reference):
        if reference:
            return PortalResult(
                "submitted",
                "Ghala verified the visible GACA acceptance page and FlightDeck "
                "found the regulator reference.",
                reference,
            )
        return PortalResult(
            "accepted_pending_reference",
            "Ghala verified a literal GACA acceptance message. The reference "
            "will be reconciled from the page, SMS, or email.",
        )
    if state == "error" and confidence >= 0.70:
        suffix = f" Ghala's assessment: {summary}" if summary else ""
        return PortalResult(
            "error",
            "Ghala verified that the visible post-Submit page is an error or "
            "non-acceptance state; the complaint is safe to retry." + suffix,
            retry_safe=True,
        )
    return None




def _await_confirmation(page, before_url: str, update,
                        before_text: str = "",
                        payload: dict | None = None,
                        timeout_seconds: int = 120,
                        submission_capture: dict | None = None,
                        ignore_initial_expiry: bool = False) -> PortalResult:
    started = time.monotonic()
    deadline = started + timeout_seconds
    waiting_reported = False
    is_gaca = bool(payload and payload.get("kind") == "gaca")
    # GACA escalations for Saudia flights still carry airline_code=SV; do not
    # apply Saudia's production-API confirmation rules to the GACA portal.
    is_saudia = bool(payload and payload.get("airline_code") == "SV"
                     and not is_gaca)
    while time.monotonic() < deadline:
        if (is_saudia and _verification_expired(page)
                and not (ignore_initial_expiry
                         and time.monotonic() - started < 2)):
            return PortalResult(
                "verification_expired",
                "Saudia expired the verification token before accepting the "
                "complaint. A fresh verification and submit attempt are required.")
        saudia_result = (_saudia_submission_result(submission_capture)
                         if is_saudia else None)
        if saudia_result:
            if saudia_result.status == "verification_expired":
                return saudia_result
            # Give Angular a brief opportunity to render the specific validation
            # error before classifying a generic rejected API response.
            if not (saudia_result.status == "error"
                    and time.monotonic() - started < 2):
                if saudia_result.status == "error":
                    return saudia_result
                update(
                    saudia_result.status,
                    saudia_result.message,
                    _page_screenshot(page) if not page.is_closed() else None)
                return saudia_result
        gaca_result = (_gaca_submission_result(submission_capture, payload)
                       if is_gaca else None)
        if gaca_result:
            if gaca_result.status in {
                    "submitted", "accepted_pending_reference"}:
                update(
                    gaca_result.status,
                    gaca_result.message,
                    _page_screenshot(page) if not page.is_closed() else None)
            return gaca_result
        gaca_verification_result = (
            _gaca_verification_result(submission_capture, payload)
            if is_gaca else None
        )
        if gaca_verification_result:
            if gaca_verification_result.status in {
                    "submitted", "accepted_pending_reference"}:
                update(
                    gaca_verification_result.status,
                    gaca_verification_result.message,
                    _page_screenshot(page) if not page.is_closed() else None,
                )
            return gaca_verification_result
        if page.is_closed():
            if is_saudia:
                return PortalResult(
                    "error",
                    "Saudia's production form closed without a verified "
                    "acceptance response or airline reference. The attempt is "
                    "recorded as failed.")
            if is_gaca:
                return PortalResult(
                    "error",
                    "GACA closed before returning any acceptance signal or "
                    "reference. No verified submission exists; the durable job "
                    "will reconcile email/SMS and retry.",
                    retry_safe=True,
                )
            return PortalResult(
                "confirmation_unknown",
                "The form was sent once, but the portal closed before a "
                "confirmation could be read.")
        if _request_blocked(page):
            if is_saudia:
                return PortalResult(
                    "error",
                    "Saudia blocked the confirmation page before its production "
                    "service verified acceptance. The attempt is recorded as "
                    "failed, not submitted.")
            if is_gaca:
                waf_error = _gaca_waf_abort(
                    payload, page, update, after_submit=True)
                return PortalResult(
                    "error",
                    str(waf_error),
                    retry_safe=True,
                    error_code=waf_error.code,
                )
            return PortalResult(
                "confirmation_unknown",
                "The form was sent once, but the official site blocked the "
                "confirmation page. FlightDeck will not submit it again.")
        text = _body_text(page)
        reference = (
            _extract_reference(text)
            or _extract_reference_from_url(page.url)
            or (_extract_gaca_reference_from_url(page.url) if is_gaca else "")
        )
        try:
            submit_still_visible = _visible(
                page.get_by_role("button", name=re.compile(r"^Submit$", re.I)))
        except Exception:
            submit_still_visible = False
        # GACA's complaint form greets the passenger with "Thank you, {name}"
        # before submission. Only treat thank-you text as success when the
        # final Submit control is gone (or a real reference appeared).
        success = bool(re.search(
            r"successfully submitted|request (?:was )?received|"
            r"complaint (?:was )?received|تم (?:استلام|إرسال)|رقم (?:الطلب|الشكوى)",
            text, re.I))
        if not success and re.search(r"thank you|شكرا", text, re.I):
            success = (not submit_still_visible) or bool(reference)
        new_reference = reference and reference not in before_text
        if new_reference or success or re.search(r"success|thank", page.url, re.I):
            if is_gaca and not reference:
                result = PortalResult(
                    "accepted_pending_reference",
                    "GACA displayed a readable submission confirmation. "
                    "FlightDeck will reconcile the regulator reference from "
                    "SMS or email and will not submit a duplicate.")
                update(
                    result.status,
                    result.message,
                    _page_screenshot(page))
                return result
            if is_saudia and not reference:
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
        # GACA: if Submit is still on-screen after the click, the portal did not
        # accept the post — refresh captcha instead of waiting out the timeout.
        if is_gaca and submit_still_visible and time.monotonic() - started >= 8:
            return PortalResult(
                "verification_expired",
                "GACA still shows the Submit form after the attempt; verification "
                "must be refreshed before a safe retry.")
        human = _needs_human_step(page)
        if human:
            pending_kind = _pending_captcha_kind(page)
            otp_visible = _visible(_otp_fields(page))
            if (is_saudia and _pending_captcha_kind(page)
                    and time.monotonic() - started >= 2):
                return PortalResult(
                    "verification_expired",
                    "Saudia requires a fresh verification before the complaint "
                    "can be submitted again.")
            if is_gaca and pending_kind == "recaptcha":
                # GACA executes its invisible v3 token inside the native Submit
                # handler. Give that handler time to navigate to email
                # verification before declaring the token stale.
                if time.monotonic() - started < 8:
                    time.sleep(1)
                    continue
                return PortalResult(
                    "verification_expired",
                    "GACA requires a fresh verification before the complaint "
                    "can be submitted again.")
            update("verification", human)
            if _VERIFICATION_HANDLER and _wait_for_human_step(
                    page,
                    update,
                    recipient_email=str((payload or {}).get("email") or ""),
            ):
                update("submitting", "Verification complete. Waiting for confirmation…")
                if is_gaca and pending_kind == "recaptcha":
                    # After a mid-wait captcha solve, Submit must be clicked again.
                    return PortalResult(
                        "verification_expired",
                        "GACA verification was refreshed; a safe re-submit is required.")
                if is_gaca and otp_visible:
                    page.wait_for_timeout(700)
                continue
        elif not waiting_reported and time.monotonic() - started > 12:
            waiting_reported = True
            update(
                "submitting",
                "The complaint was sent once. Waiting for the official "
                "confirmation without clicking Submit again.",
                _page_screenshot(page))
        time.sleep(1)
    if is_saudia:
        return PortalResult(
            "error",
            "Saudia returned neither a verified production acceptance nor an "
            "airline reference. The attempt is recorded as failed and may be "
            "retried safely.")
    if is_gaca:
        ai_result = _gaca_ai_submission_result(
            page, payload,
            "No reference or literal acceptance message appeared before the "
            "confirmation timeout.")
        if ai_result:
            return ai_result
        return PortalResult(
            "error",
            "GACA did not expose a readable confirmation or reference after "
            "Submit. No verified submission exists; FlightDeck will reconcile "
            "email/SMS and retry.",
            retry_safe=True,
        )
    return PortalResult(
        "confirmation_unknown",
        "The complaint was sent once, but the official portal did not expose "
        "a readable confirmation. FlightDeck will not submit it again.")


def _submit_with_captcha_recovery(page, payload: dict, update) -> PortalResult:
    """Submit once, but safely reacquire an expired CAPTCHA token when needed."""
    is_gaca = payload.get("kind") == "gaca"
    is_saudia = payload.get("airline_code") == "SV" and not is_gaca
    submission_capture = {} if (is_saudia or is_gaca) else None
    if submission_capture is not None:
        if is_gaca:
            page.on(
                "response",
                lambda response: _capture_gaca_response(
                    response, submission_capture))
        else:
            page.on(
                "response",
                lambda response: _capture_saudia_response(
                    response, submission_capture))
    # GACA's v3 handler performs an asynchronous native POST. A stale-looking
    # DOM is not permission to click Submit again; explicit rejections are
    # replayed later by the durable queue and ambiguous POSTs are reconciled.
    # A rejected GACA token can cause the regulator WAF to block the entire
    # residential exit.  Solve once before Submit and let the durable queue
    # rotate/cool down after an explicit rejection; never make a second POST
    # inside the same browser session.
    attempts = (
        1 + _MAX_CAPTCHA_SUBMIT_RETRIES
        if is_saudia else 1
    )
    for attempt in range(attempts):
        if attempt:
            _reset_recaptcha(page)
            page.wait_for_timeout(700)
        native_gaca_recaptcha = _use_gaca_native_recaptcha(
            page, is_gaca=is_gaca, attempt=attempt)
        if native_gaca_recaptcha:
            update(
                "verification",
                "Using GACA's native invisible verification on Submit…")
            _settle_gaca_native_recaptcha(page, update)
        elif not _wait_for_human_step(
                page,
                update,
                recipient_email=str(payload.get("email") or ""),
        ):
            if attempt + 1 < attempts and _CAPTCHA_SOLVER and _pending_captcha_kind(page):
                update(
                    "verification",
                    "CAPTCHA was not accepted. Refreshing verification and retrying…",
                    _page_screenshot(page))
                continue
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
        clicked = False
        live_manual_submit = bool(
            is_gaca and os.environ.get(
                "FLIGHTBOT_GACA_LIVE_MANUAL_SUBMIT",
                "").strip().casefold() in {"1", "true", "yes", "on"})
        if live_manual_submit:
            update(
                "reviewing",
                "Live GACA form is complete and paused before Submit. "
                "Waiting for the visible Submit button to be clicked once…",
                _page_screenshot(page),
            )
            manual_deadline = time.monotonic() + 900
            while time.monotonic() < manual_deadline:
                if submission_capture and submission_capture.get("seen"):
                    clicked = True
                    break
                try:
                    submit_visible = _visible(page.get_by_role(
                        "button", name=re.compile(r"^Submit$", re.I)))
                except Exception:
                    submit_visible = False
                if page.url != before_url or not submit_visible:
                    clicked = True
                    break
                time.sleep(0.25)
            if not clicked:
                return PortalResult(
                    "needs_attention",
                    "Live GACA debug timed out before Submit was clicked. "
                    "Nothing was sent.",
                    retry_safe=True,
                )
        elif is_gaca:
            os_gaca_input = gaca_normal_browser.os_input_enabled()
            # Prefer the complaint wizard Submit over unrelated page forms.
            # Re-assert airline right before Submit — Selectize can clear it
            # while the captcha token is acquired.
            airline = _gaca_airline_label(payload)
            if airline:
                try:
                    airline_el = page.locator(
                        "select#airline, select[name='airline'], select#airlineId")
                    empty = (not airline_el.count() or not str(
                        airline_el.first.input_value() or "").strip())
                except Exception:
                    empty = True
                if empty:
                    _select_gaca_dropdown(page, r"^airline", airline)
                    page.wait_for_timeout(400)
            form = page.locator("form").filter(
                has=page.locator(
                    "#flightNumber, #flightDate, textarea[name*='complaint' i], "
                    "#complaintDetails, [id*='complaintDetails' i], "
                    "textarea[name*='details' i]"))
            form_submit = form.get_by_role(
                "button", name=re.compile(r"^Submit$", re.I))
            if (not native_gaca_recaptcha and form.count()
                    and _captcha_completed(page)):
                clicked = _submit_gaca_form_with_injected_token(form.first)
            if not clicked and _visible(form_submit):
                try:
                    form_submit.first.scroll_into_view_if_needed()
                    if os_gaca_input:
                        gaca_normal_browser.physical_click(
                            page, form_submit.first)
                    else:
                        form_submit.first.click()
                    clicked = True
                except Exception as exc:
                    if os_gaca_input:
                        logger.exception(
                            "GACA OS-level Submit failed")
                        return PortalResult(
                            "error",
                            "FlightDeck could not complete GACA's physical "
                            f"Submit click ({type(exc).__name__}). Nothing "
                            "was sent and the durable job can retry safely.",
                            retry_safe=True,
                        )
                    clicked = False
            if not clicked and form.count() and not os_gaca_input:
                try:
                    form.first.evaluate("f => f.requestSubmit()")
                    clicked = True
                except Exception:
                    clicked = False
        if not clicked:
            clicked = _click(page, [
                "Submit", "Send", "File complaint", "Submit request",
                "إرسال", "تقديم"])
        if clicked and is_gaca:
            # Give the portal a moment to navigate or show validation.
            page.wait_for_timeout(1500)
            try:
                still = _visible(page.get_by_role(
                    "button", name=re.compile(r"^Submit$", re.I)))
            except Exception:
                still = False
            if still:
                detail = (_gaca_step2_invalid_summary(page)
                          or _validation_summary(page))
                if detail:
                    update(
                        "verification",
                        "GACA rejected Submit with validation still open: "
                        f"{detail[:240]}",
                        _page_screenshot(page))
                    if _gaca_security_rejected(detail):
                        return PortalResult(
                            "verification_expired",
                            "GACA explicitly rejected the security token. The "
                            "complaint was not accepted and is safe to retry.")
                    return PortalResult(
                        "needs_attention",
                        "GACA kept the form open because validation failed: "
                        f"{detail[:300]}",
                        retry_safe=True)
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
                    "complaint was sent.",
                    retry_safe=True)

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
            portal = "GACA" if is_gaca else "Saudia"
            if is_gaca:
                capture_seen = bool(
                    submission_capture and submission_capture.get("seen"))
                capture_status = int(
                    (submission_capture or {}).get("status") or 0)
                capture_blob = "\n".join((
                    str((submission_capture or {}).get("text") or ""),
                    str((submission_capture or {}).get("location") or ""),
                ))
                update(
                    "verification",
                    "GACA did not accept the single Submit attempt "
                    f"(POST observed: {'yes' if capture_seen else 'no'}"
                    f"{f', HTTP {capture_status}' if capture_status else ''}; "
                    "explicit security rejection: "
                    f"{'yes' if _gaca_security_rejected(capture_blob) else 'no'}). "
                    "No duplicate Submit will be sent in this session.",
                    _page_screenshot(page) if not page.is_closed() else None,
                )
            return PortalResult(
                "error",
                f"{portal} expired verification repeatedly. The complaint was "
                f"not accepted after {attempts} safe attempt"
                f"{'s' if attempts != 1 else ''} and can be retried later.",
                retry_safe=True)
        portal = "GACA" if is_gaca else "Saudia"
        update(
            "verification",
            f"{portal} expired verification before accepting the complaint. "
            f"Refreshing it with 2Captcha and retrying automatically "
            f"({attempt + 1}/{attempts - 1})…",
            _page_screenshot(page))
    return PortalResult(
        "error", "The portal submission did not complete.", retry_safe=True)


def _use_gaca_native_recaptcha(page, *, is_gaca: bool,
                               attempt: int) -> bool:
    """Keep GACA v3 in the same residential browser and IP session.

    2Captcha's supported v3 task is proxyless. GACA rejects that otherwise
    valid token as a failed security check when the complaint POST comes from
    a different residential session. Other portals still use the configured
    solver normally.
    """
    configured = os.environ.get(
        "FLIGHTBOT_GACA_NATIVE_RECAPTCHA", "1").strip().casefold()
    enabled = configured not in {"0", "false", "no", "off"}
    challenge = _recaptcha_challenge(page) if is_gaca else None
    page_context = _recaptcha_page_context(page, "") if is_gaca else {}
    return bool(
        is_gaca and attempt == 0
        and enabled
        and (
            (isinstance(challenge, dict)
             and challenge.get("kind") == "recaptcha"
             and challenge.get("is_v3"))
            or page_context.get("is_v3")))


def _open_official_page(page, url: str, payload: dict) -> None:
    """Open Saudia as soon as its form is usable; retain a safe load fallback."""
    direct_saudia = (payload.get("airline_code") == "SV"
                     and "complaint-form" in urlparse(url).path.casefold())
    is_gaca = (payload.get("kind") == "gaca"
               or "myeservices.gaca.gov.sa" in urlparse(url).netloc.casefold())
    if not direct_saudia:
        # GACA E-Services is slow from the VPS; use a longer goto budget and
        # exponential backoff instead of failing the whole escalation once.
        attempts = 3 if is_gaca else 1
        timeout_ms = 120000 if is_gaca else 60000
        last_error = None
        for attempt in range(attempts):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3500)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                page.wait_for_timeout(2000 * (2 ** attempt))
        if last_error is not None:
            raise last_error
        return

    page.goto(url, wait_until="commit", timeout=60000)
    booking = page.get_by_label(re.compile("booking reference", re.I))
    ready = _wait_for_any_visible(page, booking, 15000)
    if ready is None:
        # Slow/WAF-checked loads still receive the original conservative wait.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        _wait_for_any_visible(page, booking, 6000)
    page.wait_for_timeout(500)


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
        gaca_proxy = ""
        if payload.get("kind") == "gaca":
            gaca_proxy = (
                os.environ.get("FLIGHTBOT_GACA_PROXY", "").strip()
                or os.environ.get("FLIGHTBOT_BROWSER_PROXY", "").strip()
            )
        external_gaca = bool(
            payload.get("kind") == "gaca"
            and gaca_normal_browser.enabled())
        if external_gaca:
            _normal_browser, context = gaca_normal_browser.connect(
                playwright,
                profile_dir=_GACA_PROFILE_DIR,
            )
        else:
            context = _launch_context(
                playwright,
                proxy_server=gaca_proxy,
                profile_dir=(
                    _GACA_PROFILE_DIR
                    if payload.get("kind") == "gaca"
                    else _PROFILE_DIR),
                block_service_workers=payload.get("kind") == "gaca",
            )
        if payload.get("kind") == "gaca":
            _install_gaca_https_upgrade(context)
        page = (
            gaca_normal_browser.gaca_page(context)
            if external_gaca
            else (
                context.pages[0]
                if context.pages else context.new_page()
            )
        )
        if payload.get("kind") == "gaca":
            _attach_gaca_network_diagnostics(page)
        try:
            update(
                "opening",
                (
                    "Opening GACA in FlightDeck's persistent normal Chrome "
                    "session…"
                    if external_gaca else
                    "Opening the official website in Microsoft Edge…"
                ),
            )
            _open_official_page(page, url, payload)
            if _request_blocked(page) and (
                    payload.get("airline_code") == "SV"
                    or payload.get("kind") == "gaca"):
                site = "GACA" if payload.get("kind") == "gaca" else "Saudia"
                for attempt in range(2):
                    update(
                        "opening",
                        f"{site}'s security page has not released the form "
                        "yet. Waiting before a safe reload; nothing has "
                        "been submitted.",
                        _page_screenshot(page))
                    page.wait_for_timeout(5000 + attempt * 2000)
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4500)
                    if not _request_blocked(page):
                        break
            if _request_blocked(page):
                if payload.get("kind") == "gaca":
                    raise _gaca_waf_abort(payload, page, update)
                site = "official site"
                return PortalResult(
                    "needs_attention",
                    f"The {site} blocked the VPS browser request "
                    "(WAF/error page). FlightDeck stopped instead of treating "
                    "the block page as a complaint form.")
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
                if payload.get("kind") == "gaca":
                    raise _gaca_waf_abort(payload, page, update)
                return PortalResult(
                    "needs_attention",
                    "The official site blocked the VPS browser while preparing the form.")

            update(
                "reviewing",
                "Finished filling the known details. Checking verification and required fields.",
                _page_screenshot(page))
            if not _wait_for_human_step(
                    page,
                    update,
                    defer_captcha=True,
                    recipient_email=str(payload.get("email") or ""),
            ):
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
            if (external_gaca
                    and payload.pop(
                        "_gaca_restart_normal_browser", False)):
                try:
                    _normal_browser.close()
                except Exception:
                    logger.warning(
                        "Could not close normal GACA Chrome after proxy "
                        "rotation.",
                        exc_info=True,
                    )
            elif not external_gaca:
                context.close()
