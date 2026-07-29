"""Read-only synchronization of complaints from a signed-in GACA account.

The official account is treated as the authoritative source for regulator
references and statuses. Mapping is deterministic: exact references, airline
references, tickets, PNRs, flight numbers, dates, and passenger names are used
before any imported case is allowed to update a local complaint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

from . import db, gaca_normal_browser, portal_automation
from .config import TELEGRAM_EVIDENCE_DIR, passenger_profile_key


logger = logging.getLogger(__name__)

_PUBLIC_REFERENCE_RE = re.compile(
    r"(?<![A-Z0-9_])C[\s-]?(\d{6,})(?!\d)", re.I)
_AIRLINE_REFERENCE_RE = re.compile(
    r"(?<![A-Z0-9])C[\s_-]+(\d{6,})(?!\d)", re.I)
_FLIGHT_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2,3})[\s-]?(\d{2,4})(?!\d)", re.I)
_TICKET_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")
_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)|"
    r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})(?!\d)")


@dataclass(frozen=True)
class GacaAccountSyncResult:
    status: str
    message: str
    cases_seen: int = 0
    cases_mapped: int = 0
    cases_reconciled: int = 0
    cases_ambiguous: int = 0
    new_cases: int = 0
    screenshot_file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _digits(value: object) -> str:
    output = []
    for character in str(value or ""):
        if not character.isdigit():
            continue
        try:
            output.append(str(unicodedata.digit(character)))
        except (TypeError, ValueError):
            pass
    return "".join(output)


def _search_key(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(
        character.casefold() for character in value
        if character.isalnum()
    )


def _reference(value: object) -> str:
    match = _PUBLIC_REFERENCE_RE.search(str(value or ""))
    return f"C{match.group(1)}" if match else ""


def _airline_reference(value: object) -> str:
    match = _AIRLINE_REFERENCE_RE.search(str(value or ""))
    return f"C_{match.group(1)}" if match else ""


def _flight_number(value: object) -> str:
    match = _FLIGHT_RE.search(str(value or ""))
    return f"{match.group(1).upper()}{match.group(2)}" if match else ""


def _ticket_number(value: object) -> str:
    match = _TICKET_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _date(value: object) -> str:
    text = str(value or "").strip()
    match = _DATE_RE.search(text)
    if not match:
        for pattern in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(text, pattern).date().isoformat()
            except ValueError:
                continue
        return ""
    if match.group(1):
        year, month, day = map(int, match.group(1, 2, 3))
    else:
        day, month, year = map(int, match.group(4, 5, 6))
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def _normalized_pairs(pairs: list[dict] | list[list] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs or []:
        if isinstance(pair, dict):
            label = pair.get("label")
            value = pair.get("value")
        elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
            label, value = pair[0], pair[1]
        else:
            continue
        key = _search_key(label)
        clean_value = re.sub(r"\s+", " ", str(value or "")).strip()
        if key and clean_value:
            if key in result and clean_value not in result[key]:
                result[key] += " | " + clean_value
            else:
                result[key] = clean_value
    return result


def _labeled(fields: dict[str, str], *patterns: str) -> str:
    for key, value in fields.items():
        if any(re.search(pattern, key, re.I) for pattern in patterns):
            return value
    return ""


def normalize_gaca_case(record: dict) -> dict | None:
    """Normalize a listing row or detail page into one regulator case."""
    text = re.sub(r"\s+", " ", str(record.get("text") or "")).strip()
    fields = _normalized_pairs(record.get("pairs"))
    all_values = " ".join(fields.values())
    blob = " ".join((text, all_values))

    reference_source = _labeled(
        fields,
        r"^(?:complaint|request|case)?(?:reference|number|id)$",
        r"complaintnumber", r"requestnumber", r"referencenumber",
        r"رقمالشكوى", r"الرقمالمرجعي", r"رقمالطلب",
    )
    reference = _reference(reference_source)
    if not reference:
        public_matches = {
            f"C{match.group(1)}" for match in _PUBLIC_REFERENCE_RE.finditer(blob)
        }
        if len(public_matches) == 1:
            reference = public_matches.pop()

    airline_ref_source = _labeled(
        fields,
        r"airline.*(?:reference|complaint|ticket|case)",
        r"(?:reference|complaint|ticket|case).*airline",
        r"carrier.*(?:reference|complaint|ticket|case)",
        r"مرجع.*شركة", r"شكوى.*شركة",
    )
    airline_ref = _airline_reference(airline_ref_source)
    if not airline_ref:
        matches = {
            f"C_{match.group(1)}"
            for match in _AIRLINE_REFERENCE_RE.finditer(blob)
        }
        if len(matches) == 1:
            airline_ref = matches.pop()

    flight_source = _labeled(
        fields, r"flightnumber", r"flightno", r"رقمالرحلة")
    flight = _flight_number(flight_source)
    if not flight:
        flights = {_flight_number(match.group(0))
                   for match in _FLIGHT_RE.finditer(blob)}
        flights.discard("")
        if len(flights) == 1:
            flight = flights.pop()

    ticket_source = _labeled(
        fields, r"ticketnumber", r"eticket", r"رقمالتذكرة")
    ticket = _ticket_number(ticket_source)
    if not ticket:
        tickets = {match.group(1) for match in _TICKET_RE.finditer(blob)}
        if len(tickets) == 1:
            ticket = tickets.pop()

    date_source = _labeled(
        fields, r"flightdate", r"traveldate", r"dateofflight",
        r"تاريخالرحلة")
    flight_date = _date(date_source)
    submitted_source = _labeled(
        fields, r"submittedat", r"submissiondate", r"createdat",
        r"complaintdate", r"requestdate", r"تاريخالشكوى", r"تاريخالطلب")
    submitted_at = _date(submitted_source) or submitted_source

    pnr = _labeled(
        fields, r"^pnr$", r"bookingreference", r"reservationnumber",
        r"رقمالحجز")
    pnr = re.sub(r"[^A-Z0-9]", "", pnr.upper())
    if not re.fullmatch(r"[A-Z0-9]{5,8}", pnr):
        pnr = ""

    case = {
        "reference": reference,
        "status": _labeled(
            fields, r"^status$", r"complaintstatus", r"requeststatus",
            r"حالةالشكوى", r"حالةالطلب"),
        "service_type": _labeled(
            fields, r"servicetype", r"requesttype", r"typeofservice",
            r"نوعالخدمة", r"نوعالطلب"),
        "airline": _labeled(
            fields, r"^airline$", r"airlinename", r"carriername",
            r"شركةالطيران", r"الناقلالجوي"),
        "flight_number": flight,
        "flight_date": flight_date,
        "airline_reference": airline_ref,
        "ticket_number": ticket,
        "pnr": pnr,
        "passenger_name": _labeled(
            fields, r"passengername", r"travelername", r"fullname",
            r"اسمالمسافر"),
        "origin": _labeled(
            fields, r"^origin$", r"departureairport", r"fromairport",
            r"مطارالمغادرة", r"من$"),
        "destination": _labeled(
            fields, r"^destination$", r"arrivalairport", r"toairport",
            r"مطارالوصول", r"إلى$"),
        "category": _labeled(
            fields, r"category", r"complainttype", r"subject",
            r"تصنيف", r"نوعالشكوى"),
        "submitted_at": submitted_at,
        "complaint_text": _labeled(
            fields, r"complainttext", r"description", r"details",
            r"complaintdetails", r"تفاصيلالشكوى", r"الوصف"),
        "source_url": str(record.get("url") or ""),
        "raw": {
            "title": str(record.get("title") or ""),
            "text": text[:30000],
            "pairs": record.get("pairs") or [],
        },
    }
    if not case["reference"] and not any((
            case["airline_reference"], case["flight_number"],
            case["ticket_number"], case["pnr"])):
        return None
    case["case_key"] = (
        case["reference"]
        or "account:" + hashlib.sha256(
            json.dumps(
                case["raw"], ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()[:32]
    )
    return case


def _effective(flight: dict, field: str):
    overrides = flight.get("overrides") or {}
    value = overrides.get(field)
    return value if value not in (None, "") else flight.get(field)


def _flight_facts(flight: dict) -> dict:
    numbers = {
        _flight_number(value)
        for value in (
            [_effective(flight, "flight_number")]
            + list(flight.get("flight_numbers") or [])
        )
    }
    numbers.discard("")
    tickets = {
        _ticket_number(value)
        for value in (flight.get("ticket_numbers") or [])
    }
    tickets.discard("")
    passenger = re.sub(
        r"\s+e[\s-]*ticket\b.*$", "",
        str(_effective(flight, "passenger") or ""),
        flags=re.I,
    ).strip()
    return {
        "flight_numbers": numbers,
        "flight_date": _date(_effective(flight, "flight_date")),
        "tickets": tickets,
        "pnr": _search_key(_effective(flight, "pnr")),
        "passenger": passenger_profile_key(passenger),
        "airline": _search_key(
            flight.get("airline_name") or flight.get("airline_code")),
        "origin": _search_key(_effective(flight, "origin")),
        "destination": _search_key(_effective(flight, "destination")),
    }


def _score_flight(case: dict, flight: dict) -> tuple[int, list[str]]:
    facts = _flight_facts(flight)
    score, methods = 0, []
    if case.get("ticket_number") and case["ticket_number"] in facts["tickets"]:
        score += 650
        methods.append("ticket")
    if case.get("pnr") and _search_key(case["pnr"]) == facts["pnr"]:
        score += 360
        methods.append("pnr")
    case_flight = _flight_number(case.get("flight_number"))
    if case_flight and case_flight in facts["flight_numbers"]:
        score += 240
        methods.append("flight")
    case_date = _date(case.get("flight_date"))
    if case_date and case_date == facts["flight_date"]:
        score += 190
        methods.append("date")
    case_passenger = passenger_profile_key(case.get("passenger_name") or "")
    if case_passenger and case_passenger == facts["passenger"]:
        score += 180
        methods.append("passenger")
    case_airline = _search_key(case.get("airline"))
    if (case_airline and facts["airline"]
            and (case_airline in facts["airline"]
                 or facts["airline"] in case_airline)):
        score += 70
        methods.append("airline")
    for key in ("origin", "destination"):
        wanted = _search_key(case.get(key))
        if wanted and facts[key] and (
                wanted in facts[key] or facts[key] in wanted):
            score += 45
            methods.append(key)
    return score, methods


def _clear_winner(scored: list[tuple[int, list[str], dict]],
                  minimum: int) -> tuple[dict | None, int, list[str], bool]:
    scored = sorted(scored, key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < minimum:
        return None, scored[0][0] if scored else 0, [], False
    best_score, methods, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    clear = len(scored) == 1 or best_score - runner_up >= 120
    return (best if clear else None), best_score, methods, not clear


def map_gaca_case(
        case: dict,
        complaints: list[dict],
        flights: list[dict],
) -> dict:
    """Map an account case without using names or model guesses alone."""
    by_complaint_id = {
        int(item["id"]): item for item in complaints if item.get("id")
    }
    flight_by_key = {
        str(item.get("flight_key") or ""): item for item in flights
    }
    public_ref = _reference(case.get("reference"))
    airline_ref = _airline_reference(case.get("airline_reference"))
    candidates = []
    for complaint in complaints:
        if complaint.get("kind") != "gaca":
            continue
        existing_ref = _reference(complaint.get("reference"))
        if existing_ref and public_ref and existing_ref != public_ref:
            continue
        score, methods = 0, []
        if public_ref and existing_ref == public_ref:
            score += 1200
            methods.append("gaca_reference")
        parent = by_complaint_id.get(
            int(complaint.get("parent_complaint_id") or 0))
        if (airline_ref and parent
                and _airline_reference(parent.get("reference")) == airline_ref):
            score += 800
            methods.append("airline_reference")
        flight = (
            complaint.get("flight_data")
            or flight_by_key.get(str(complaint.get("flight_key") or ""))
            or {}
        )
        flight_score, flight_methods = _score_flight(case, flight)
        score += flight_score
        methods.extend(flight_methods)
        candidates.append((score, methods, complaint))
    # Flight number + exact travel date is sufficient only when it identifies
    # one clear local case. Shared itineraries remain ambiguous below.
    winner, score, methods, ambiguous = _clear_winner(candidates, 400)
    if winner:
        return {
            "status": "mapped",
            "complaint_id": int(winner["id"]),
            "flight_key": str(winner.get("flight_key") or ""),
            "method": "+".join(methods),
            "score": score,
        }

    flight_scores = []
    for flight in flights:
        flight_score, flight_methods = _score_flight(case, flight)
        flight_scores.append((flight_score, flight_methods, flight))
    flight, flight_score, flight_methods, flight_ambiguous = _clear_winner(
        flight_scores, 400)
    if flight:
        return {
            "status": "flight_only",
            "complaint_id": None,
            "flight_key": str(flight.get("flight_key") or ""),
            "method": "+".join(flight_methods),
            "score": flight_score,
        }
    return {
        "status": "ambiguous" if ambiguous or flight_ambiguous else "unmapped",
        "complaint_id": None,
        "flight_key": "",
        "method": "",
        "score": max(score, flight_score),
    }


def _page_snapshot(page) -> dict:
    return page.evaluate(
        """() => {
          const clean = value => String(value || '')
            .replace(/\\s+/g, ' ').trim();
          const pairs = [];
          const add = (label, value) => {
            label = clean(label); value = clean(value);
            if (label && value && label !== value && value.length < 12000) {
              pairs.push({label, value});
            }
          };
          document.querySelectorAll('tr').forEach(row => {
            const cells = Array.from(row.querySelectorAll(':scope > th, :scope > td'));
            if (cells.length === 2) add(cells[0].innerText, cells[1].innerText);
          });
          document.querySelectorAll('dt').forEach(label => {
            const value = label.nextElementSibling;
            if (value && value.matches('dd')) add(label.innerText, value.innerText);
          });
          document.querySelectorAll('label').forEach(label => {
            const controlId = label.getAttribute('for');
            const control = controlId ? document.getElementById(controlId) : null;
            if (control) {
              add(label.innerText, control.value || control.innerText);
              return;
            }
            const parent = label.parentElement;
            const value = parent && parent.querySelector(
              'input, select, textarea, [class*="value"], p, span');
            if (value && value !== label) {
              add(label.innerText, value.value || value.innerText);
            }
          });
          const links = Array.from(document.querySelectorAll('a[href]')).map(link => ({
            text: clean(link.innerText || link.getAttribute('aria-label')),
            href: link.href
          }));
          const headers = Array.from(
            document.querySelectorAll('table thead th')).map(cell => clean(cell.innerText));
          const records = Array.from(document.querySelectorAll(
            'table tbody tr, [class*="request-card"], [class*="complaint-card"], ' +
            '[class*="application-card"], [class*="case-card"]'
          )).map(node => {
            const cells = Array.from(node.querySelectorAll(':scope > td'));
            const rowPairs = [];
            cells.forEach((cell, index) => {
              if (headers[index]) rowPairs.push({
                label: headers[index], value: clean(cell.innerText)
              });
            });
            const rowLinks = Array.from(node.querySelectorAll('a[href]')).map(link => ({
              text: clean(link.innerText || link.getAttribute('aria-label')),
              href: link.href
            }));
            return {
              text: clean(node.innerText),
              pairs: rowPairs,
              links: rowLinks
            };
          }).filter(record => record.text);
          return {
            url: location.href,
            title: document.title,
            text: clean(document.body && document.body.innerText),
            pairs,
            links,
            records
          };
        }""")


def _same_gaca_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == gaca_normal_browser.GACA_HOST
    )


def _candidate_url(link: dict, *, detail: bool = False) -> bool:
    href = str(link.get("href") or "")
    text = " ".join((str(link.get("text") or ""), href)).casefold()
    if not _same_gaca_url(href):
        return False
    if re.search(r"/(?:login|logout|register|forget-password)(?:/|$)", href, re.I):
        return False
    if re.search(r"\b(?:new|create|submit|apply)\b", text):
        return False
    if detail:
        return bool(re.search(
            r"detail|view|track|status|request|complaint|case|application|"
            r"تفاصيل|طلب|شكوى",
            text, re.I))
    return bool(re.search(
        r"dashboard|my.?requests?|my.?complaints?|requests?|complaints?|"
        r"applications?|cases?|track|لوحة|طلباتي|شكاوى",
        text, re.I))


def _collect_account_records(page, max_cases: int) -> list[dict]:
    """Traverse only account links actually exposed by the signed-in portal."""
    origin = gaca_normal_browser.GACA_HOME
    page.goto(origin, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)
    queue = [str(page.url)]
    visited: set[str] = set()
    detail_urls: list[str] = []
    normalized: dict[str, dict] = {}

    while queue and len(visited) < 12:
        url = queue.pop(0)
        if url in visited or not _same_gaca_url(url):
            continue
        visited.add(url)
        if str(page.url) != url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(900)
        if portal_automation._is_gaca_login_page(page):
            raise PermissionError("GACA account sign-in expired during sync.")
        snapshot = _page_snapshot(page)
        records = list(snapshot.get("records") or [])
        records.append({
            "text": snapshot.get("text"),
            "pairs": snapshot.get("pairs"),
            "links": snapshot.get("links"),
        })
        for record in records:
            record["url"] = snapshot.get("url")
            record["title"] = snapshot.get("title")
            case = normalize_gaca_case(record)
            if case:
                normalized[case["case_key"]] = case
            for link in record.get("links") or []:
                href = str(link.get("href") or "")
                if _candidate_url(link, detail=True) and href not in detail_urls:
                    detail_urls.append(href)
        for link in snapshot.get("links") or []:
            href = str(link.get("href") or "")
            if _candidate_url(link) and href not in visited and href not in queue:
                queue.append(href)

    for url in detail_urls:
        if len(normalized) >= max_cases or url in visited:
            continue
        visited.add(url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)
        if portal_automation._is_gaca_login_page(page):
            raise PermissionError("GACA account sign-in expired during sync.")
        snapshot = _page_snapshot(page)
        case = normalize_gaca_case(snapshot)
        if case:
            normalized[case["case_key"]] = case
    return list(normalized.values())[:max_cases]


def import_gaca_cases(cases: list[dict]) -> GacaAccountSyncResult:
    """Map, save, and safely reconcile already-normalized account cases."""
    complaints = db.list_complaints()
    flights = db.list_flights()
    mapped = reconciled = ambiguous = new = 0
    for case in cases:
        mapping = map_gaca_case(case, complaints, flights)
        if mapping["status"] in {"mapped", "flight_only"}:
            mapped += 1
        elif mapping["status"] == "ambiguous":
            ambiguous += 1
        if db.upsert_gaca_account_case(case, mapping):
            new += 1
        complaint_id = mapping.get("complaint_id")
        if complaint_id and case.get("reference"):
            local = next(
                (item for item in complaints
                 if int(item.get("id") or 0) == int(complaint_id)),
                None,
            )
            local_ref = _reference((local or {}).get("reference"))
            if not local_ref and db.reconcile_gaca_account_case(
                    case["case_key"], int(complaint_id), case["reference"]):
                reconciled += 1
    message = (
        f"Imported {len(cases)} GACA case(s): {mapped} mapped to local "
        f"flight records, {reconciled} recovered official reference(s), "
        f"and {ambiguous} left for review."
    )
    return GacaAccountSyncResult(
        "success", message, len(cases), mapped, reconciled, ambiguous, new)


def sync_gaca_account(
        config: dict,
        update: Callable[[str, str, bytes | None], None] | None = None,
        *,
        allow_login: bool = False,
) -> GacaAccountSyncResult:
    """Read the GACA account using the same persistent browser as submissions."""
    settings = config.get("gaca_account") or {}
    if not settings.get("enabled", True):
        result = GacaAccountSyncResult(
            "disabled", "GACA account synchronization is disabled.")
        db.save_gaca_account_sync(result.status, result.message)
        return result

    def progress(stage: str, message: str, image: bytes | None = None):
        if update:
            update(stage, message, image)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result = GacaAccountSyncResult(
            "error", "Browser automation is not installed.")
        db.save_gaca_account_sync(result.status, result.message)
        return result

    max_cases = max(1, min(int(settings.get("max_cases", 200)), 1000))
    screenshot_file = ""
    try:
        with portal_automation._BROWSER_LOCK:
            with sync_playwright() as playwright:
                if not gaca_normal_browser.enabled():
                    raise RuntimeError(
                        "The persistent normal GACA browser is not enabled.")
                _browser, context = gaca_normal_browser.connect(
                    playwright, profile_dir=portal_automation._GACA_PROFILE_DIR)
                page = gaca_normal_browser.gaca_page(context)
                progress(
                    "opening",
                    "Opening your signed-in GACA account in read-only mode.")
                page.goto(
                    gaca_normal_browser.GACA_HOME,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(1000)
                if portal_automation._is_gaca_login_page(page):
                    if not allow_login:
                        result = GacaAccountSyncResult(
                            "auth_required",
                            "The saved GACA session expired. Send /gaca in "
                            "Telegram to start a fresh Nafath login.")
                        db.save_gaca_account_sync(
                            result.status, result.message)
                        return result
                    progress(
                        "verification",
                        "The GACA session expired. Starting Nafath sign-in.")
                    if not portal_automation._wait_for_gaca_login(
                            page, progress, config.get("user") or {}):
                        result = GacaAccountSyncResult(
                            "auth_required",
                            "GACA sign-in was not completed. Nothing was "
                            "submitted or changed.")
                        db.save_gaca_account_sync(
                            result.status, result.message)
                        return result
                progress(
                    "filling",
                    "Signed in. Reading your GACA complaint list and details.")
                cases = _collect_account_records(page, max_cases)
                screenshot = page.screenshot(type="png", full_page=False)
                folder = TELEGRAM_EVIDENCE_DIR / "gaca_account"
                folder.mkdir(parents=True, exist_ok=True)
                destination = folder / "latest.png"
                temporary = destination.with_suffix(".png.tmp")
                temporary.write_bytes(screenshot)
                temporary.replace(destination)
                screenshot_file = str(destination)
        result = import_gaca_cases(cases)
        result = GacaAccountSyncResult(
            **{**result.to_dict(), "screenshot_file": screenshot_file})
        db.save_gaca_account_sync(
            result.status, result.message,
            cases_seen=result.cases_seen,
            cases_mapped=result.cases_mapped,
            cases_reconciled=result.cases_reconciled,
            cases_ambiguous=result.cases_ambiguous,
            screenshot_file=result.screenshot_file,
        )
        progress("submitted", result.message)
        return result
    except PermissionError as exc:
        result = GacaAccountSyncResult(
            "auth_required", str(exc), screenshot_file=screenshot_file)
    except Exception as exc:
        logger.exception("GACA account synchronization failed")
        result = GacaAccountSyncResult(
            "error",
            f"GACA account synchronization stopped safely: {exc}",
            screenshot_file=screenshot_file,
        )
    db.save_gaca_account_sync(
        result.status, result.message,
        screenshot_file=result.screenshot_file or None)
    progress("error", result.message)
    return result
