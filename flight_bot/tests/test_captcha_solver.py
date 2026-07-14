from copy import deepcopy

from flight_bot.captcha_solver import TwoCaptchaSolver
from flight_bot.config import DEFAULTS


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append((url, json, timeout))
        return Response(next(self.responses))


def test_2captcha_creates_polls_and_returns_recaptcha_token():
    config = deepcopy(DEFAULTS)
    config["captcha"].update({
        "enabled": True,
        "api_key": "test-key",
        "poll_interval_seconds": 5,
        "timeout_seconds": 60,
    })
    session = Session([
        {"errorId": 0, "taskId": 12345},
        {"errorId": 0, "status": "processing"},
        {
            "errorId": 0,
            "status": "ready",
            "solution": {"gRecaptchaResponse": "solved-token"},
            "cost": "0.00299",
        },
    ])
    waits = []
    solver = TwoCaptchaSolver(config, session=session, sleeper=waits.append)
    result = solver.solve_recaptcha({
        "website_url": "https://www.saudia.com/form",
        "site_key": "site-key",
        "user_agent": "Modern Browser",
        "api_domain": "recaptcha.net",
        "is_invisible": False,
    })

    assert result == {
        "token": "solved-token", "task_id": "12345", "cost": "0.00299"}
    assert waits == [5, 5]
    create = session.requests[0][1]
    assert create["clientKey"] == "test-key"
    assert create["task"] == {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": "https://www.saudia.com/form",
        "websiteKey": "site-key",
        "isInvisible": False,
        "userAgent": "Modern Browser",
        "apiDomain": "recaptcha.net",
    }
    assert session.requests[1][1]["taskId"] == 12345
