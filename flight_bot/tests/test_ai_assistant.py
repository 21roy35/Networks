import json
from copy import deepcopy

from flight_bot.ai_assistant import ClaudeAssistant
from flight_bot.config import DEFAULTS


class FakeResponse:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError("request failed")

    def json(self):
        return self.value


class FakeSession:
    def __init__(self, value, status=200):
        self.response = FakeResponse(value, status)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def enabled_config():
    config = deepcopy(DEFAULTS)
    config["ai"].update({
        "enabled": True, "api_key": "test-anthropic-key",
        "name": "Ghala-200", "model": "claude-sonnet-5",
    })
    return config


def test_incident_uses_structured_output_without_changing_original_facts(tmp_path):
    result = {
        "category": "seat", "summary": "The passenger reported a broken seat.",
        "facts": ["The seat was broken"],
        "evidence_observations": [],
        "requested_remedy": "Investigate and provide applicable remedies.",
        "severity": "medium", "needs_more_info": False,
        "follow_up_question": "",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)
    evidence = tmp_path / "seat.jpg"
    evidence.write_bytes(b"jpeg-evidence")
    actual = assistant.analyze_incident(
        "The seat was broken.", {"flight_number": "SV 100", "pnr": "ABC123"},
        [str(evidence)])
    assert actual == result
    _url, request = session.calls[0]
    assert request["headers"]["x-api-key"] == "test-anthropic-key"
    assert request["json"]["model"] == "claude-sonnet-5"
    assert request["json"]["output_config"]["format"]["type"] == "json_schema"
    content = request["json"]["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    prompt = content[-1]["text"]
    assert "The seat was broken." in prompt
    assert "Never invent" in request["json"]["system"]


def test_portal_vision_places_png_before_the_untrusted_page_text():
    decision = {
        "state": "needs_navigation", "summary": "Continue is available.",
        "action": "click", "target": "Continue", "value": "",
        "confidence": 0.94, "user_prompt": "",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(decision)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)
    assert assistant.portal_decision({
        "image": b"png-bytes", "page_url": "https://example.test/form",
        "page_text": "Continue", "elements": [], "payload": {"pnr": "ABC123"},
    }) == decision
    content = session.calls[0][1]["json"]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"


def test_ai_failure_is_non_fatal_and_does_not_expose_response_data():
    assistant = ClaudeAssistant(
        enabled_config(), session=FakeSession({"error": "sensitive"}, status=500))
    assert assistant.analyze_response("subject", "body", "CASE-1", "Airline") is None
    assert "sensitive" not in assistant.last_error
