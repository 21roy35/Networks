"""Configuration loading.

Reads config.json from the project root (see config.example.json) and
allows sensitive values to be supplied via environment variables so
credentials never need to be committed:

    FLIGHTBOT_IMAP_HOST, FLIGHTBOT_IMAP_USER, FLIGHTBOT_IMAP_PASSWORD
    FLIGHTBOT_SMTP_HOST, FLIGHTBOT_SMTP_USER, FLIGHTBOT_SMTP_PASSWORD
"""

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DB_PATH = PROJECT_ROOT / "flightbot.db"
SAMPLE_EMAILS_DIR = PROJECT_ROOT / "sample_emails"

DEFAULTS = {
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "user": "",
        "password": "",
        "folders": ["INBOX"],
        "since_days": 730,
    },
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "",
        "password": "",
    },
    "user": {
        "full_name": "",
        "email": "",
        "phone": "",
        "national_id": "",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 5000,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    config = DEFAULTS
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            config = _deep_merge(config, json.load(fh))

    env_map = {
        ("imap", "host"): "FLIGHTBOT_IMAP_HOST",
        ("imap", "user"): "FLIGHTBOT_IMAP_USER",
        ("imap", "password"): "FLIGHTBOT_IMAP_PASSWORD",
        ("smtp", "host"): "FLIGHTBOT_SMTP_HOST",
        ("smtp", "user"): "FLIGHTBOT_SMTP_USER",
        ("smtp", "password"): "FLIGHTBOT_SMTP_PASSWORD",
    }
    for (section, key), env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[section][key] = value

    # Sensible fallbacks: reuse IMAP account for SMTP if not set.
    if not config["smtp"]["user"]:
        config["smtp"]["user"] = config["imap"]["user"]
    if not config["smtp"]["password"]:
        config["smtp"]["password"] = config["imap"]["password"]
    if not config["user"]["email"]:
        config["user"]["email"] = config["imap"]["user"]
    return config
