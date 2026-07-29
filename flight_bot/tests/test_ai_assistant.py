import json
from copy import deepcopy

from flight_bot.ai_assistant import ClaudeAssistant
from flight_bot.config import DEFAULTS


class FakeResponse:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status
        self.status_code = status

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
    assert request["json"]["thinking"] == {"type": "disabled"}
    assert request["json"]["output_config"]["format"]["type"] == "json_schema"
    content = request["json"]["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    prompt = content[-1]["text"]
    assert "The seat was broken." in prompt
    assert "natural first-person account" in prompt
    assert "4 to 7 sentences" in prompt
    assert "obvious direct practical impact" in prompt
    assert "Add three to five distinct, natural everyday implications" in prompt
    assert "harder to get into and out of the seat" in prompt
    assert "stiffness, back or neck discomfort, fatigue, dizziness" in prompt
    assert "dizziness or motion discomfort as something the fixed position made harder" in prompt
    assert "consume time in tracking or replacing essentials" in prompt
    assert "never say 'the passenger'" in prompt
    assert "Do not repeat the airline, flight number, date, route" in prompt
    assert "omit all complaint reference numbers and complaint dates" in prompt
    assert "Never calculate or state how many days have elapsed" in prompt
    assert "do not say they were supplied, completed, or submitted" in prompt
    assert "never introduce compensation" in prompt
    assert "does not by itself prove that an item was ruined" in prompt
    assert "Only mention buying clothes, toiletries, or a value such as 300 SAR" in prompt
    assert "Never turn 'delayed baggage' into a claim that the bag was later returned" in prompt
    assert "Never say an issue lasted the entire flight" in prompt
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


def test_sms_distillation_copies_only_visible_otp_and_reference():
    result = {
        "otp": "839201", "reference": "C_2777654",
        "kind": "otp", "summary": "Code and case reference found.",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)
    actual = assistant.distill_sms(
        "SAUDIA", "Use 839201. Complaint reference C_2777654.")

    assert actual["otp"] == "839201"
    assert actual["reference"] == "C_2777654"
    prompt = session.calls[0][1]["json"]["messages"][0]["content"][-1]["text"]
    assert "Hey, I have ADHD" in prompt


def test_portal_failure_explanation_is_grounded_in_job_and_screenshot():
    result = {
        "visible_state": "The CAPTCHA checkbox is unchecked.",
        "likely_cause": "Verification was still pending at submission time.",
        "current_status": "The job is recorded as failed.",
        "next_step": "Resolve verification before one safe retry.",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)

    actual = assistant.analyze_portal_failure(
        "Why did it fail?", {
            "status": "error", "message": "Submission rejected",
            "reference": "", "airline_code": "SV",
            "automatic_captcha_enabled": True,
            "telegram_fallback_enabled": True,
        }, [{"direction": "incoming", "text": "I see the captcha"}],
        image=b"png-bytes")

    assert actual == result
    content = session.calls[0][1]["json"]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "text"]
    assert "Only a non-empty reference" in content[-1]["text"]
    assert "do not tell the user that they must solve" in content[-1]["text"]


def test_structured_output_accepts_thinking_before_the_json_text():
    result = {
        "category": "service", "summary": "Service issue.",
        "facts": ["A service issue was reported"],
        "evidence_observations": [],
        "requested_remedy": "Investigate.", "severity": "low",
        "needs_more_info": False, "follow_up_question": "",
    }
    session = FakeSession({"content": [
        {"type": "thinking", "thinking": "internal"},
        {"type": "text", "text": json.dumps(result)},
    ]})
    assistant = ClaudeAssistant(enabled_config(), session=session)
    assert assistant.analyze_incident(
        "The cabin service was unavailable.", {}) == result


def test_incident_prompt_does_not_turn_a_document_request_into_compliance():
    result = {
        "category": "baggage", "summary": "The baggage issue remains unresolved.",
        "facts": ["The airline requested documents"],
        "evidence_observations": [],
        "requested_remedy": "I am asking for a fair resolution.",
        "severity": "medium", "needs_more_info": False,
        "follow_up_question": "",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)

    assert assistant.analyze_incident(
        "The airline requested a form and documents, but the case is unresolved.",
        {},
        case_context={"portal_destination": "gaca"},
    ) == result

    prompt = session.calls[0][1]["json"]["messages"][0]["content"][-1]["text"]
    assert "Trusted program-generated grounding constraints" in prompt
    assert "does not confirm that the passenger supplied them" in prompt
    assert "Do not copy any airline complaint reference number" in prompt


def test_ai_failure_is_non_fatal_and_does_not_expose_response_data():
    assistant = ClaudeAssistant(
        enabled_config(), session=FakeSession({"error": "sensitive"}, status=500))
    assert assistant.analyze_response("subject", "body", "CASE-1", "Airline") is None
    assert "sensitive" not in assistant.last_error


def test_low_credit_error_is_actionable_without_copying_api_response():
    response = {"error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low. purchase credits. secret detail",
    }}
    session = FakeSession(response, status=400)
    assistant = ClaudeAssistant(enabled_config(), session=session)
    assert assistant.analyze_response("subject", "body", "CASE-1", "Airline") is None
    assert assistant.last_error == "Anthropic credit balance is too low"
    assert "secret detail" not in assistant.last_error
    assert assistant.analyze_response(
        "subject", "body", "CASE-1", "Airline") is None
    assert len(session.calls) == 1


def test_profile_extraction_accepts_only_labeled_verbatim_evidence():
    result = {"fields": [
        {
            "field": "national_id", "value": "1122334455",
            "source_index": 0,
            "evidence_excerpt": "National ID: 1122334455",
        },
        {
            "field": "alfursan_id", "value": "0652200741431",
            "source_index": 0,
            "evidence_excerpt": "e-Ticket: 0652200741431",
        },
        {
            "field": "nationality", "value": "Saudi",
            "source_index": 0,
            "evidence_excerpt": "Flight from Saudi Arabia",
        },
        {
            "field": "title", "value": "Miss", "source_index": 0,
            "evidence_excerpt": "Miss Lujain Alasais",
        },
    ]}
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)
    actual = assistant.extract_passenger_profile("Lujain Alasais", [{
        "source": "PDF attachment: ticket.pdf",
        "text": ("Miss Lujain Alasais e-Ticket: 0652200741431 "
                 "National ID: 1122334455 Flight from Saudi Arabia"),
    }])

    assert actual["values"] == {
        "national_id": "1122334455", "title": "Ms"}
    assert actual["evidence"]["national_id"].startswith(
        "Ghala-200 verified in PDF attachment")
    prompt = session.calls[0][1]["json"]["messages"][0]["content"][-1]["text"]
    assert "exactly the named passenger" in prompt
    assert "Lujain Alasais" in prompt


def test_telegram_intent_selects_tools_without_answering_from_catalog():
    result = {
        "actions": [{
            "name": "flight_details", "flight_number": "SV1671",
            "pnr": "", "reference": "", "passenger": "Mansour",
            "query": "", "time_scope": "past", "latest": False,
            "limit": 5,
        }],
        "reply": "",
    }
    session = FakeSession({
        "content": [{"type": "text", "text": json.dumps(result)}],
    })
    assistant = ClaudeAssistant(enabled_config(), session=session)

    actual = assistant.interpret_telegram(
        "Show me Mansour's SV1671 details", {
            "counts": {"flights": 3},
            "flights": [{
                "flight_number": "SV1671", "passenger": "Mansour Alasais",
            }],
        })

    assert actual == result
    request = session.calls[0][1]["json"]
    schema = request["output_config"]["format"]["schema"]
    names = schema["properties"]["actions"]["items"]["properties"]["name"]["enum"]
    assert "show_evidence" in names
    assert "scan_mailbox" in names
    prompt = request["messages"][0]["content"][-1]["text"]
    assert "intent router, not a data reasoning task" in prompt
    assert "never claim that a lookup succeeded" in prompt
