"""Configuration loading with environment-variable credential overrides."""

import json
import os
from copy import deepcopy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DB_PATH = PROJECT_ROOT / "flightbot.db"
SAMPLE_EMAILS_DIR = PROJECT_ROOT / "sample_emails"
TELEGRAM_EVIDENCE_DIR = PROJECT_ROOT / "telegram_evidence"

DEFAULTS = {
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "user": "",
        "password": "",
        "folders": ["INBOX"],
        "since_days": 730,
    },
    "user": {
        "full_name": "",
        "title": "",
        "nationality": "",
        "country_code": "",
        "email": "",
        "phone": "",
        "national_id": "",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 5000,
        "public_base_url": "",
        "access_secret": "",
        "link_expiry_minutes": 15,
        "session_days": 30,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "poll_timeout_seconds": 25,
        "monitor_interval_seconds": 60,
        "post_flight_delay_minutes": 20,
        "survey_lookback_hours": 24,
        "complaint_debounce_seconds": 20,
        "verification_timeout_minutes": 10,
        "mailbox_scan_minutes": 10,
    },
    "flight_status": {
        "provider": "schedule",
        "flightaware_api_key": "",
        "poll_minutes": 10,
    },
    "captcha": {
        "enabled": False,
        "provider": "2captcha",
        "api_key": "",
        "poll_interval_seconds": 5,
        "timeout_seconds": 180,
        "telegram_fallback": True,
    },
    "ai": {
        "enabled": False,
        "provider": "anthropic",
        "name": "Ghala-200",
        "model": "claude-sonnet-5",
        "api_key": "",
        "timeout_seconds": 45,
        "analyze_incidents": True,
        "analyze_attachments": True,
        "analyze_responses": True,
        "portal_assistance": True,
        "max_portal_attempts": 3,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Invalid JSON in {CONFIG_PATH} at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{CONFIG_PATH} must contain a JSON object.")
    return value


def load_config() -> dict:
    # Always start with an independent copy so values cannot leak between
    # app or test instances.
    config = _deep_merge(DEFAULTS, _load_file())

    env_map = {
        ("imap", "host"): "FLIGHTBOT_IMAP_HOST",
        ("imap", "user"): "FLIGHTBOT_IMAP_USER",
        ("imap", "password"): "FLIGHTBOT_IMAP_PASSWORD",
        ("telegram", "bot_token"): "FLIGHTBOT_TELEGRAM_BOT_TOKEN",
        ("telegram", "chat_id"): "FLIGHTBOT_TELEGRAM_CHAT_ID",
        ("flight_status", "flightaware_api_key"): "FLIGHTBOT_FLIGHTAWARE_API_KEY",
        ("captcha", "api_key"): "FLIGHTBOT_2CAPTCHA_API_KEY",
        ("web", "public_base_url"): "FLIGHTBOT_PUBLIC_BASE_URL",
        ("web", "access_secret"): "FLIGHTBOT_WEB_ACCESS_SECRET",
        ("ai", "api_key"): "FLIGHTBOT_ANTHROPIC_API_KEY",
        ("ai", "model"): "FLIGHTBOT_AI_MODEL",
        ("ai", "name"): "FLIGHTBOT_AI_NAME",
    }
    for (section, key), env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[section][key] = value

    if not config["user"]["email"]:
        config["user"]["email"] = config["imap"]["user"]

    for section, key in (("imap", "port"), ("web", "port")):
        try:
            config[section][key] = int(config[section][key])
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{section}.{key} must be a number.") from exc
    if not isinstance(config["imap"].get("folders"), list):
        raise SystemExit("imap.folders must be a JSON list of mailbox names.")
    if config["telegram"].get("bot_token") and config["telegram"].get("chat_id"):
        config["telegram"]["enabled"] = True
    if config["captcha"].get("api_key"):
        config["captcha"]["enabled"] = True
    if config["ai"].get("api_key"):
        config["ai"]["enabled"] = True
    return config


def save_user_profile(profile: dict) -> None:
    """Persist only reusable complaint profile fields, preserving all settings."""
    current = _load_file()
    existing = current.get("user")
    if not isinstance(existing, dict):
        existing = {}
    allowed = {
        "full_name", "email", "phone", "national_id", "title",
        "nationality", "country_code",
    }
    current["user"] = {
        **existing,
        **{key: str(value or "").strip() for key, value in profile.items()
           if key in allowed},
    }
    temporary = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(CONFIG_PATH)
