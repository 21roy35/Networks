"""Read GACA case outcomes through the regulator's SMS Details workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import requests


GACA_DETAILS_URL = "https://pxpticket.gaca.gov.sa/"
GACA_API_ROOT = "https://pxpwebapi.gaca.gov.sa/webapi/api"
GACA_RECAPTCHA_SITE_KEY = "6LeZeewrAAAAAJb5RDm3Awm39yKz0nflqGPPb5ER"

_STATUS_LABELS = {
    850980009: "rejected",
    850980008: "closed",
    850980004: "closed",
    5: "solved",
    1: "in_progress",
    850980007: "canceled",
    850980018: "opened",
    850980003: "needs_information",
    850980005: "directed_to_department",
}


class GacaStatusError(RuntimeError):
    """The official GACA checker could not return a verified case."""


@dataclass(frozen=True)
class GacaCaseResult:
    status: str
    case_status: str
    response_text: str
    message: str
    data: dict


@dataclass(frozen=True)
class GacaRemediation:
    """One explicit next step grounded in GACA's verified response."""

    action: str
    reason: str
    requested_information: str = ""
    suggested_category: str = ""
    automatic: bool = False


def extract_gaca_details_url(value: str) -> str:
    """Return only GACA's official SMS Details URL."""
    for candidate in re.findall(r"https?://[^\s<>\"]+", str(value or ""), re.I):
        candidate = candidate.rstrip(".,،؛;)")
        parsed = urlparse(candidate)
        if (
            parsed.scheme.casefold() == "https"
            and (parsed.hostname or "").casefold() == "pxpticket.gaca.gov.sa"
        ):
            return GACA_DETAILS_URL
    return ""


def normalize_gaca_phone(country_code: str, phone: str) -> str:
    """Return the international phone format used by GACA's React client."""
    raw_phone = re.sub(r"\D", "", str(phone or ""))
    raw_country = re.sub(r"\D", "", str(country_code or ""))
    if not raw_phone:
        return ""
    if raw_country and raw_phone.startswith(raw_country):
        return "+" + raw_phone
    if not raw_country and raw_phone.startswith("966"):
        return "+" + raw_phone
    if raw_country:
        return "+" + raw_country + raw_phone.lstrip("0")
    return "+" + raw_phone


def requires_airline_complaint(response_text: str) -> bool:
    """Return whether GACA says the airline must receive the complaint first."""
    value = " ".join(str(response_text or "").split())
    return bool(re.search(
        r"(?:must|should|required to)\s+(?:first\s+)?"
        r"(?:file|submit|raise|lodge).{0,80}(?:complaint|case)"
        r".{0,100}(?:airline|air carrier)|"
        r"(?:file|submit|raise|lodge).{0,80}(?:complaint|case)"
        r".{0,100}(?:airline|air carrier).{0,100}(?:first|before)|"
        r"(?:supplied|provided|airline|carrier).{0,50}"
        r"(?:reference|number).{0,50}(?:is\s+)?not.{0,30}"
        r"(?:complaint|case)\s+(?:number|reference)|"
        r"(?:airline|carrier).{0,40}(?:complaint|case).{0,40}"
        r"(?:number|reference).{0,40}(?:invalid|incorrect)|"
        r"يجب\s+أولاً\s+تقديم\s+الشكوى\s+لدى\s+الناقل\s+الجوي|"
        r"تقديم\s+الشكوى\s+(?:أولاً\s+)?(?:إلى|لدى)\s+"
        r"(?:شركة|الناقل)\s+(?:الطيران|الجوي)",
        value,
        re.I,
    ))


def interpret_gaca_remediation(
        response_text: str,
        *,
        case_status: str = "",
        data: dict | None = None) -> GacaRemediation:
    """Map a verified GACA result to one bounded complaint action."""
    data = data or {}
    value = " ".join(str(response_text or "").split())
    if requires_airline_complaint(value):
        return GacaRemediation(
            "airline_prerequisite",
            "GACA explicitly requires a fresh complaint with the airline first.",
            automatic=True,
        )

    wrong_category = re.search(
        r"\b(?:wrong|incorrect|invalid|inappropriate)\s+"
        r"(?:complaint\s+)?(?:category|classification|type)\b|"
        r"\b(?:re-?submit|re-?file|file)\b.{0,100}"
        r"\b(?:correct|appropriate)\s+(?:category|classification)\b|"
        r"(?:التصنيف|الفئة|نوع\s+الشكوى).{0,30}"
        r"(?:غير\s+صحيح|خاطئ|غير\s+مناسب)|"
        r"(?:إعادة|اعدادة|اعد)\s+تقديم.{0,80}"
        r"(?:التصنيف|الفئة)\s+(?:الصحيح|المناسب)",
        value,
        re.I,
    )
    if wrong_category:
        suggested = ""
        match = re.search(
            r"(?:re-?submit|re-?file|file)\s+(?:it\s+)?under\s+"
            r"(?:the\s+)?(?:category\s*)?[:\-]?\s*"
            r"[\"']?([^.;\n]{3,100})|"
            r"(?:suggested|correct|appropriate)\s+"
            r"(?:category|classification)\s*[:\-]\s*"
            r"[\"']?([^.;\n]{3,100})|"
            r"(?:category|classification)\s*:\s*"
            r"[\"']?([^.;\n]{3,100})|"
            r"(?:ضمن|تحت)\s+(?:تصنيف|فئة)\s*[:\-]?\s*"
            r"[\"']?([^،.;\n]{3,100})",
            value,
            re.I,
        )
        if match:
            suggested = " ".join(
                str(next(
                    (group for group in match.groups() if group),
                    "",
                )).split()
            ).strip(" \"'")
        return GacaRemediation(
            "correct_category",
            "GACA explicitly says the complaint used the wrong category.",
            suggested_category=suggested,
            automatic=True,
        )

    info_note = " ".join(str(
        data.get("notesRegardingAdditionalInfoFromTheTravel") or ""
    ).split())
    if not info_note:
        match = re.search(
            r"(?:Additional-information request|additional information"
            r"(?: required| requested)?|معلومات\s+إضافية|بيانات\s+إضافية)"
            r"\s*[:\-]\s*(.{3,1200})",
            value,
            re.I,
        )
        if match:
            info_note = " ".join(match.group(1).split())
    if info_note or str(case_status).casefold() == "needs_information":
        return GacaRemediation(
            "provide_information",
            "GACA is waiting for additional information from the passenger.",
            requested_information=info_note,
            automatic=False,
        )

    if str(case_status).casefold() in {"solved"}:
        return GacaRemediation(
            "record_resolution",
            "GACA marked the complaint solved.",
            automatic=True,
        )
    return GacaRemediation(
        "review",
        "No explicit corrective filing instruction was found.",
        automatic=False,
    )


def _response_json(response) -> object:
    try:
        return response.json()
    except (TypeError, ValueError):
        return None


def _otp_request_id(response) -> str:
    value = _response_json(response)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get("success") is False:
            return ""
        candidate = value.get("id") or value.get("otpId") or value.get("data")
        if isinstance(candidate, dict):
            candidate = candidate.get("id") or candidate.get("otpId")
        return str(candidate or "").strip()
    text = str(getattr(response, "text", "") or "").strip().strip('"')
    return text if re.fullmatch(r"[A-Za-z0-9_-]{8,200}", text) else ""


def _status_label(value) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value or "unknown").strip().casefold() or "unknown"
    return _STATUS_LABELS.get(numeric, f"status_{numeric}")


def _case_response_text(data: dict, case_status: str) -> str:
    fields = (
        ("Case status", case_status),
        ("Provided solution", data.get("standardReplyDetails")),
        ("GACA response", data.get("inquiryResponse")),
        ("Additional-information request",
         data.get("notesRegardingAdditionalInfoFromTheTravel")),
        ("Main category", data.get("category") or data.get("categoryAR")),
        ("Subcategory", data.get("subCategory") or data.get("subCategoryAR")),
    )
    return "\n".join(
        f"{label}: {' '.join(str(value).split())}"
        for label, value in fields
        if str(value or "").strip()
    )


def check_gaca_case(
    reference: str,
    phone: str,
    *,
    captcha_solver: Callable[[dict], dict | None],
    verification_handler: Callable[[dict], object],
    session=None,
    proxy_url: str = "",
    update: Callable[[str, str], None] | None = None,
) -> GacaCaseResult:
    """Complete GACA's official reCAPTCHA + SMS OTP status check."""
    reference = str(reference or "").strip().upper()
    phone = str(phone or "").strip()
    if not re.fullmatch(r"C\d{6,12}", reference):
        raise GacaStatusError("A valid GACA complaint reference is required.")
    if not re.fullmatch(r"\+\d{9,15}", phone):
        raise GacaStatusError("The registered GACA phone number is unavailable.")
    if not callable(captcha_solver):
        raise GacaStatusError("2Captcha is not configured for GACA status checks.")
    if not callable(verification_handler):
        raise GacaStatusError("The SMS OTP relay is not available.")

    update = update or (lambda *_args: None)
    session = session or requests.Session()
    proxy_url = str(proxy_url or "").strip()
    if proxy_url:
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })
    update("verification", f"Solving GACA verification for {reference}…")
    solved = captcha_solver({
        "kind": "recaptcha",
        "website_url": GACA_DETAILS_URL,
        "site_key": GACA_RECAPTCHA_SITE_KEY,
        "is_v3": False,
        "is_invisible": False,
        "api_domain": "google.com",
    }) or {}
    token = str(solved.get("token") or "").strip()
    if not token:
        raise GacaStatusError("2Captcha returned no GACA verification token.")

    update("otp", f"Requesting GACA's OTP for {reference}…")
    try:
        otp_response = session.post(
            f"{GACA_API_ROOT}/PxpTicket/SendOtpToCaseOwner",
            headers={
                "Content-Type": "application/json",
                "reCAPTCHA-Token": token,
            },
            json={"mobileNumber": phone, "hashCode": reference},
            timeout=45,
        )
    except requests.RequestException as exc:
        raise GacaStatusError("GACA's OTP service could not be reached.") from exc

    if int(getattr(otp_response, "status_code", 0) or 0) == 401:
        return GacaCaseResult(
            "in_progress",
            "in_progress",
            "Case status: in_progress",
            "GACA reports that the complaint is still under review.",
            {},
        )
    if not getattr(otp_response, "ok", False):
        status_code = int(getattr(otp_response, "status_code", 0) or 0)
        if status_code == 400:
            raise GacaStatusError(
                "GACA did not accept the registered phone number for this case.")
        raise GacaStatusError(
            f"GACA's OTP service returned HTTP {status_code or 'error'}.")

    otp_request_id = _otp_request_id(otp_response)
    if not otp_request_id:
        raise GacaStatusError("GACA did not return an OTP request identifier.")
    otp = re.sub(r"\D", "", str(verification_handler({
        "kind": "otp",
        "message": (
            f"GACA sent a one-time code while FlightDeck checks {reference}. "
            "The SMS shortcut should enter it automatically; reply with the "
            "four digits only if needed."
        ),
        "image": b"",
        "url": GACA_DETAILS_URL,
    }) or ""))
    if not re.fullmatch(r"\d{4,8}", otp):
        raise GacaStatusError("The GACA status-check OTP was not received.")

    try:
        validated = session.post(
            f"{GACA_API_ROOT}/PxpTicket/ValidateOtp",
            headers={"Content-Type": "application/json"},
            json={"id": otp_request_id, "otp": otp},
            timeout=45,
        )
    except requests.RequestException as exc:
        raise GacaStatusError("GACA's OTP validation could not be reached.") from exc
    validation_value = _response_json(validated)
    if not getattr(validated, "ok", False) or validation_value is not True:
        raise GacaStatusError("GACA rejected the status-check OTP.")

    update("checking", f"Reading GACA's verified response for {reference}…")
    try:
        case_response = session.get(
            f"{GACA_API_ROOT}/PxpTicket/casebyid/{reference}",
            params={"phoneNumber": phone},
            timeout=45,
        )
    except requests.RequestException as exc:
        raise GacaStatusError("GACA's case-details service could not be reached.") from exc
    payload = _response_json(case_response)
    if not getattr(case_response, "ok", False) or not isinstance(payload, dict):
        raise GacaStatusError("GACA did not return readable case details.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GacaStatusError("GACA returned no case record for this reference.")

    case_status = _status_label(data.get("status"))
    response_text = _case_response_text(data, case_status)
    message = (
        f"GACA case {reference} is {case_status.replace('_', ' ')}."
        + (
            f" {str(data.get('standardReplyDetails')).strip()}"
            if str(data.get("standardReplyDetails") or "").strip()
            else ""
        )
    )
    return GacaCaseResult(
        case_status,
        case_status,
        response_text,
        message,
        data,
    )
