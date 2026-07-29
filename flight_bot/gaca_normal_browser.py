"""Normal-Chrome and OS-input helpers for GACA's risk-scored portal.

GACA uses reCAPTCHA v3.  A Playwright-launched browser can receive a low risk
score even when every form field is valid.  This module starts the installed
Chrome binary normally, keeps a dedicated persistent profile, attaches over
CDP only after launch, and uses X11 mouse/keyboard input for the final
submission and email-verification actions.
"""

from __future__ import annotations

import logging
import hashlib
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


logger = logging.getLogger(__name__)

GACA_HOST = "myeservices.gaca.gov.sa"
GACA_HOME = "https://myeservices.gaca.gov.sa/eservices/"
_NORMAL_CHROME_PROCESS: subprocess.Popen | None = None
_PROXY_SESSION_MARKER = ".flightdeck-gaca-proxy-session"


def enabled() -> bool:
    value = os.environ.get(
        "FLIGHTBOT_GACA_NORMAL_BROWSER", "").strip().casefold()
    return value in {"1", "true", "yes", "on"}


def os_input_enabled() -> bool:
    value = os.environ.get(
        "FLIGHTBOT_GACA_OS_INPUT", "").strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _cdp_port() -> int:
    value = os.environ.get(
        "FLIGHTBOT_GACA_NORMAL_CDP_PORT", "9225").strip()
    if not re.fullmatch(r"\d{2,5}", value):
        raise RuntimeError("FLIGHTBOT_GACA_NORMAL_CDP_PORT is invalid.")
    return int(value)


def _cdp_ready(port: int) -> bool:
    try:
        with urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1.0) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def _chrome_binary() -> str:
    configured = os.environ.get(
        "FLIGHTBOT_GACA_CHROME_BINARY", "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise RuntimeError(
            "FLIGHTBOT_GACA_CHROME_BINARY does not exist.")
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        if path := shutil.which(name):
            return path
    raise RuntimeError("The installed Google Chrome binary was not found.")


def _profile_dir(default: Path) -> Path:
    configured = os.environ.get(
        "FLIGHTBOT_GACA_NORMAL_PROFILE_DIR", "").strip()
    path = Path(configured) if configured else default
    path.mkdir(parents=True, exist_ok=True)
    return path


def _local_proxy() -> str:
    value = os.environ.get(
        "FLIGHTBOT_GACA_LOCAL_PROXY", "").strip()
    if not value:
        raise RuntimeError(
            "FLIGHTBOT_GACA_LOCAL_PROXY is required for normal Chrome.")
    return value


def _chrome_environment() -> dict[str, str]:
    """Keep Chrome's real locale signals aligned with the Saudi exit IP.

    Normal Chrome inherits the VPS timezone unless we set it explicitly.
    The GACA proxy exits in Riyadh Region, while the host itself runs in UTC;
    exposing that contradiction to reCAPTCHA v3 needlessly lowers trust in an
    otherwise ordinary browser session.
    """
    environment = os.environ.copy()
    environment["TZ"] = (
        os.environ.get("FLIGHTBOT_GACA_TIMEZONE", "").strip()
        or "Asia/Riyadh"
    )
    return environment


def _wait_for_cdp(port: int, process: subprocess.Popen | None) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if _cdp_ready(port):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"Normal Chrome exited with status {process.returncode}.")
        time.sleep(0.25)
    raise RuntimeError("Normal Chrome did not expose its CDP endpoint.")


def _proxy_session_file() -> Path | None:
    configured = os.environ.get(
        "FLIGHTBOT_GACA_PROXY_SESSION_FILE", "").strip()
    return Path(configured) if configured else None


def _proxy_session_value() -> str:
    session_file = _proxy_session_file()
    if session_file is not None:
        try:
            value = session_file.read_text(encoding="ascii").strip()
        except OSError:
            value = ""
        if value:
            return value
    return os.environ.get(
        "FLIGHTBOT_GACA_PROXY_SESSION", "").strip()


def _proxy_session_fingerprint() -> str:
    """Return a non-secret marker for the configured residential session."""
    session = _proxy_session_value()
    if not session:
        return ""
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def rotate_proxy_session() -> bool:
    """Rotate the bridge's sticky residential session without exposing it."""
    session_file = _proxy_session_file()
    if session_file is None:
        return False
    session_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = session_file.with_name(session_file.name + ".tmp")
    temporary.write_text(secrets.token_hex(12), encoding="ascii")
    temporary.chmod(0o600)
    os.replace(temporary, session_file)
    logger.warning(
        "Rotated the GACA residential proxy after a confirmed pre-Submit "
        "WAF rejection.")
    return True


def _sync_proxy_session_state(context, profile: Path) -> bool:
    """Clear only GACA state when the upstream residential session changes.

    GACA's WAF cookie is bound to the residential exit. Reusing that cookie
    after rotating the upstream session allows the public wizard's GET pages
    but rejects its first personal-information POST with HTTP 403. Keep the
    dedicated normal-Chrome profile for a realistic browser fingerprint, while
    discarding GACA cookies and site storage exactly once per proxy rotation.
    """
    current = _proxy_session_fingerprint()
    if not current:
        return False
    marker = profile / _PROXY_SESSION_MARKER
    try:
        previous = marker.read_text(encoding="ascii").strip()
    except OSError:
        previous = ""
    if previous == current:
        return False

    # Playwright supports a domain filter here, so Google/reCAPTCHA state in
    # the dedicated profile remains intact for risk scoring.
    context.clear_cookies(
        domain=re.compile(r"(?:^|\.)gaca\.gov\.sa$", re.I))
    page = next(
        (
            candidate for candidate in context.pages
            if GACA_HOST in str(candidate.url or "")
        ),
        None,
    ) or context.new_page()
    session = context.new_cdp_session(page)
    try:
        for origin in (
                "https://myeservices.gaca.gov.sa",
                "https://gaca.gov.sa",
                "https://www.gaca.gov.sa"):
            session.send("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": (
                    "cookies,local_storage,session_storage,indexeddb,"
                    "websql,cache_storage,service_workers"
                ),
            })
    finally:
        session.detach()

    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(current, encoding="ascii")
    os.replace(temporary, marker)
    logger.info(
        "Cleared GACA site state after residential proxy rotation.")
    return True


def connect(playwright, *, profile_dir: Path):
    """Return a CDP-attached normal Chrome browser and its default context."""
    global _NORMAL_CHROME_PROCESS
    port = _cdp_port()
    profile = _profile_dir(profile_dir)
    if not _cdp_ready(port):
        command = [
            _chrome_binary(),
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            f"--proxy-server={_local_proxy()}",
            "--proxy-bypass-list=<-loopback>",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--window-size=1360,900",
            GACA_HOME,
        ]
        logger.info(
            "Starting normal Chrome for GACA on CDP port %s.", port)
        _NORMAL_CHROME_PROCESS = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_chrome_environment(),
        )
        _wait_for_cdp(port, _NORMAL_CHROME_PROCESS)
    browser = playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{port}", timeout=30_000)
    if not browser.contexts:
        raise RuntimeError("Normal Chrome did not expose a browser context.")
    context = browser.contexts[0]
    _sync_proxy_session_state(context, profile)
    return browser, context


def gaca_page(context):
    """Select the existing GACA tab, or create one in normal Chrome."""
    page = next(
        (
            candidate for candidate in context.pages
            if GACA_HOST in str(candidate.url or "")
        ),
        None,
    )
    return page or context.new_page()


def _xdotool() -> str:
    path = shutil.which("xdotool")
    if not path:
        raise RuntimeError(
            "xdotool is required for GACA OS-level browser input.")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "DISPLAY is unavailable for GACA OS-level browser input.")
    return path


def _run_xdotool(*args: object, check: bool = True) -> str:
    result = subprocess.run(
        [_xdotool(), *(str(arg) for arg in args)],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


def _window_id(page) -> str:
    page.bring_to_front()
    title = str(page.title() or "").strip()
    patterns = [
        title,
        "Airline Complaint",
        "General Authority of Civil Aviation",
        "GACA",
    ]
    for pattern in patterns:
        if not pattern:
            continue
        result = _run_xdotool(
            "search", "--onlyvisible", "--name",
            re.escape(pattern), check=False)
        candidates = [
            line.strip() for line in result.splitlines()
            if line.strip().isdigit()
        ]
        if candidates:
            return candidates[-1]
    result = _run_xdotool(
        "search", "--onlyvisible", "--class",
        "google-chrome", check=False)
    candidates = [
        line.strip() for line in result.splitlines()
        if line.strip().isdigit()
    ]
    if not candidates:
        raise RuntimeError("The normal GACA Chrome window was not found.")
    return candidates[-1]


def _window_geometry(window_id: str) -> dict[str, int]:
    output = _run_xdotool(
        "getwindowgeometry", "--shell", window_id)
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"-?\d+", value.strip()):
            values[key.strip()] = int(value.strip())
    if not all(key in values for key in ("X", "Y", "WIDTH", "HEIGHT")):
        raise RuntimeError("Could not read the normal Chrome window geometry.")
    return values


def _element_geometry(page, locator) -> dict:
    locator.scroll_into_view_if_needed()
    return locator.evaluate("""element => {
        const rect = element.getBoundingClientRect();
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(centerX, centerY);
        return {
          disabled: !!element.disabled,
          visible: !!element.offsetParent,
          inViewport:
            rect.left >= 0 &&
            rect.top >= 0 &&
            rect.right <= window.innerWidth &&
            rect.bottom <= window.innerHeight,
          hitIsElement: hit === element || element.contains(hit),
          rect: {
            left: rect.left, top: rect.top,
            width: rect.width, height: rect.height
          },
          outerWidth: window.outerWidth,
          outerHeight: window.outerHeight,
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight
        };
    }""")


def _screen_point(
        browser: dict[str, int],
        element: dict,
) -> tuple[int, int]:
    scale = browser["WIDTH"] / float(element["outerWidth"])
    side_border = (
        element["outerWidth"] - element["innerWidth"]) / 2.0
    top_chrome = (
        element["outerHeight"] -
        element["innerHeight"] -
        side_border
    )
    rect = element["rect"]
    x = round(
        browser["X"] +
        (side_border + rect["left"] + rect["width"] / 2.0) * scale
    )
    y = round(
        browser["Y"] +
        (top_chrome + rect["top"] + rect["height"] / 2.0) * scale
    )
    return int(x), int(y)


def _focus(window_id: str) -> None:
    _run_xdotool("windowmap", window_id, check=False)
    _run_xdotool("windowraise", window_id, check=False)
    _run_xdotool("windowfocus", "--sync", window_id)


def _human_mouse_move(x: int, y: int) -> None:
    output = _run_xdotool("getmouselocation", "--shell")
    current = {"X": x, "Y": y}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in current and re.fullmatch(
                r"-?\d+", value.strip()):
            current[key] = int(value.strip())
    for step in range(1, 25):
        fraction = step / 24.0
        next_x = round(current["X"] + (x - current["X"]) * fraction)
        next_y = round(current["Y"] + (y - current["Y"]) * fraction)
        _run_xdotool("mousemove", "--sync", next_x, next_y)
        time.sleep(0.025)


def physical_click(page, locator) -> None:
    """Click one verified DOM element with the X11 mouse."""
    window_id = _window_id(page)
    _focus(window_id)
    geometry = _element_geometry(page, locator)
    if geometry.get("disabled") or not geometry.get("visible"):
        raise RuntimeError("The requested GACA control is not available.")
    if not geometry.get("inViewport"):
        raise RuntimeError("The requested GACA control is outside the viewport.")
    if not geometry.get("hitIsElement"):
        raise RuntimeError("The requested GACA control is covered.")
    x, y = _screen_point(_window_geometry(window_id), geometry)
    _human_mouse_move(x, y)
    _run_xdotool("click", "1")


def physical_type_otp(page, fields: list, code: str, verify) -> None:
    """Enter the short-lived GACA code and click Verify through X11."""
    if not fields or not re.fullmatch(r"\d{4,8}", code):
        raise RuntimeError("The GACA email code is invalid.")
    if len(fields) != len(code):
        raise RuntimeError(
            "The number of visible GACA OTP boxes did not match the code.")
    window_id = _window_id(page)
    _focus(window_id)
    browser_geometry = _window_geometry(window_id)
    # GACA advances focus from one maxlength=1 box to the next in ordinary
    # desktop Chrome. On X11, xdotool's bulk ``type`` command can be treated
    # as one synthetic burst and the page clears it. Click and key each box
    # independently, matching the successful Windows physical-input flow.
    matched = False
    for _attempt in range(2):
        for field, digit in zip(fields, code):
            geometry = _element_geometry(page, field)
            if not geometry.get("hitIsElement"):
                raise RuntimeError("A GACA OTP box is covered.")
            x, y = _screen_point(browser_geometry, geometry)
            _human_mouse_move(x, y)
            _run_xdotool("click", "1")
            _run_xdotool("key", "--clearmodifiers", digit)
            time.sleep(0.35)
        page.wait_for_timeout(350)
        values = [
            str(field.input_value() or "") for field in fields
        ]
        if "".join(values) == code:
            matched = True
            break
    if not matched:
        raise RuntimeError(
            "GACA OTP did not populate all visible boxes.")
    physical_click(page, verify)
