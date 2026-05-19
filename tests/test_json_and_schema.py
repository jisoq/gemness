from __future__ import annotations

from gemness.json_utils import extract_cli_response, extract_json_candidate, parse_json_candidate, strip_code_fence
from gemness.schema_validation import validate_json_schema


def test_strip_code_fence() -> None:
    assert strip_code_fence("```json\n{\"ok\": true}\n```") == '{"ok": true}'


def test_extract_json_candidate_from_prose() -> None:
    assert extract_json_candidate('text before {"a": [1, 2]} text after') == '{"a": [1, 2]}'


def test_parse_json_failure_reports_message() -> None:
    data, error, candidate = parse_json_candidate('{"a":')
    assert data is None
    assert "line 1" in error
    assert candidate == '{"a":'


def test_extract_cli_response_tolerates_warning_after_json_envelope() -> None:
    text, envelope = extract_cli_response('{"response":"ok","stats":{"tools":{"totalCalls":0}}}\\nWarning: noisy cli note')
    assert text == "ok"
    assert envelope["stats"]["tools"]["totalCalls"] == 0


def test_schema_validation_pass_and_fail() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }
    assert validate_json_schema({"name": "ok"}, schema) == []
    errors = validate_json_schema({"name": 1, "extra": True}, schema)
    assert len(errors) == 2
    assert any("not of type" in error["message"] for error in errors)
