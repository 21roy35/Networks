"""Automatic CAPTCHA solving through the 2Captcha API v2."""

from __future__ import annotations

import time

import requests


class CaptchaSolverError(RuntimeError):
    """Raised when the external solver cannot return a usable answer."""


class TwoCaptchaSolver:
    API_ROOT = "https://api.2captcha.com"

    def __init__(self, config: dict, session=None, sleeper=None):
        self.settings = config.get("captcha") or {}
        self.api_key = str(self.settings.get("api_key") or "").strip()
        self.session = session or requests.Session()
        self.sleep = sleeper or time.sleep
        self.poll_seconds = max(
            5, int(self.settings.get("poll_interval_seconds") or 5))
        self.timeout_seconds = max(
            30, min(int(self.settings.get("timeout_seconds") or 180), 600))

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled") and self.api_key)

    def _post(self, method: str, payload: dict) -> dict:
        try:
            response = self.session.post(
                f"{self.API_ROOT}/{method}", json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CaptchaSolverError(
                "2Captcha could not be reached or returned invalid data.") from exc
        if int(result.get("errorId") or 0):
            code = str(result.get("errorCode") or "2Captcha task failed")
            raise CaptchaSolverError(code)
        return result

    def _solve_task(self, task: dict, token_fields: tuple[str, ...]) -> dict:
        created = self._post("createTask", {
            "clientKey": self.api_key,
            "task": task,
        })
        task_id = created.get("taskId")
        if not task_id:
            raise CaptchaSolverError("2Captcha did not return a task ID.")

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            self.sleep(self.poll_seconds)
            result = self._post("getTaskResult", {
                "clientKey": self.api_key,
                "taskId": task_id,
            })
            if result.get("status") == "processing":
                continue
            if result.get("status") != "ready":
                raise CaptchaSolverError("2Captcha returned an unknown task status.")
            solution = result.get("solution") or {}
            token = next((str(solution.get(field) or "").strip()
                          for field in token_fields
                          if str(solution.get(field) or "").strip()), "")
            if not token:
                raise CaptchaSolverError("2Captcha returned an empty token.")
            return {
                "token": token,
                "task_id": str(task_id),
                "cost": str(result.get("cost") or ""),
            }
        raise CaptchaSolverError("2Captcha timed out before returning a token.")

    def solve(self, challenge: dict) -> dict:
        """Dispatch a rendered challenge to the matching 2Captcha task."""
        if str(challenge.get("kind") or "").lower() == "hcaptcha":
            return self.solve_hcaptcha(challenge)
        return self.solve_recaptcha(challenge)

    def solve_recaptcha(self, challenge: dict) -> dict:
        """Return a reCAPTCHA v2 token and non-secret task metadata."""
        if not self.enabled:
            raise CaptchaSolverError("2Captcha is not configured.")
        website_url = str(challenge.get("website_url") or "").strip()
        site_key = str(challenge.get("site_key") or "").strip()
        if not website_url or not site_key:
            raise CaptchaSolverError("The reCAPTCHA site key is unavailable.")

        task = {
            "type": ("RecaptchaV2EnterpriseTaskProxyless"
                     if challenge.get("is_enterprise")
                     else "RecaptchaV2TaskProxyless"),
            "websiteURL": website_url,
            "websiteKey": site_key,
            "isInvisible": bool(challenge.get("is_invisible")),
        }
        user_agent = str(challenge.get("user_agent") or "").strip()
        if user_agent:
            task["userAgent"] = user_agent
        api_domain = str(challenge.get("api_domain") or "").strip()
        if api_domain in {"google.com", "recaptcha.net"}:
            task["apiDomain"] = api_domain
        data_s = str(challenge.get("data_s") or "").strip()
        if data_s:
            task["recaptchaDataSValue"] = data_s

        return self._solve_task(task, ("gRecaptchaResponse", "token"))

    def solve_hcaptcha(self, challenge: dict) -> dict:
        """Return an hCaptcha token and non-secret task metadata."""
        if not self.enabled:
            raise CaptchaSolverError("2Captcha is not configured.")
        website_url = str(challenge.get("website_url") or "").strip()
        site_key = str(challenge.get("site_key") or "").strip()
        if not website_url or not site_key:
            raise CaptchaSolverError("The hCaptcha site key is unavailable.")

        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": site_key,
            "isInvisible": bool(challenge.get("is_invisible")),
        }
        user_agent = str(challenge.get("user_agent") or "").strip()
        if user_agent:
            task["userAgent"] = user_agent
        enterprise_payload = challenge.get("enterprise_payload")
        if isinstance(enterprise_payload, dict) and enterprise_payload:
            task["enterprisePayload"] = enterprise_payload
        return self._solve_task(task, ("gRecaptchaResponse", "token"))
