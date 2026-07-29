"""Signed, short-lived links for opening FlightDeck from Telegram."""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "flightdeck-telegram-web-v1"


def create_web_token(secret: str, chat_id: str) -> str:
    if not secret:
        raise ValueError("A web access secret is required.")
    return URLSafeTimedSerializer(secret, salt=_SALT).dumps(
        {"chat_id": str(chat_id)})


def verify_web_token(secret: str, token: str, max_age_seconds: int) -> bool:
    if not secret or not token:
        return False
    try:
        payload = URLSafeTimedSerializer(secret, salt=_SALT).loads(
            token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return bool(payload.get("chat_id"))
