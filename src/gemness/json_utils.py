from __future__ import annotations

import json
import re
from typing import Any


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def extract_json_candidate(text: str) -> str:
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)\n?```", text, re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        if candidate.startswith(("{", "[")):
            return candidate

    stripped = strip_code_fence(text)
    if stripped.startswith(("{", "[")):
        return _balanced_prefix(stripped) or stripped

    for index, char in enumerate(text):
        if char in "{[":
            candidate = _balanced_prefix(text[index:])
            if candidate:
                return candidate
    return stripped


def parse_json_candidate(text: str) -> tuple[Any | None, str | None, str]:
    candidate = extract_json_candidate(text)
    try:
        return json.loads(candidate), None, candidate
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} at line {exc.lineno} column {exc.colno}", candidate


def extract_cli_response(stdout: str) -> tuple[str, dict[str, Any] | None]:
    stripped = stdout.strip()
    if not stripped:
        return "", None
    envelope = _parse_json_prefix(stripped)
    if isinstance(envelope, dict):
        text = _extract_envelope_text(envelope)
        return (text, envelope) if text is not None else (stdout, envelope)
    embedded = _first_embedded_response_envelope(stripped)
    if embedded is not None:
        text = _extract_envelope_text(embedded)
        if text is not None:
            return text, embedded
    return stdout, None


def _parse_json_prefix(text: str) -> Any | None:
    if not text.startswith(("{", "[")):
        return None
    json_text = _balanced_prefix(text) or text
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        return None


def _first_embedded_response_envelope(text: str) -> dict[str, Any] | None:
    for match in re.finditer(r"[{[]", text):
        candidate = _balanced_prefix(text[match.start() :])
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and _extract_envelope_text(parsed) is not None:
            return parsed
    return None


def _extract_envelope_text(envelope: dict[str, Any]) -> str | None:
    for key in ("response", "text", "content", "output"):
        value = envelope.get(key)
        if isinstance(value, str):
            return value
    candidates = envelope.get("candidates")
    if isinstance(candidates, list) and candidates:
        return _extract_candidate_text(candidates[0])
    return None


def _extract_candidate_text(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    content = candidate.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            values = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
            if values:
                return "".join(values)
    return None


def _balanced_prefix(text: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escape = False
    opening = {"{": "}", "[": "]"}
    closing = {"}", "]"}
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in opening:
            stack.append(opening[char])
        elif char in closing:
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return text[: index + 1].strip()
    return None
