from __future__ import annotations

import re
from typing import Any

REDACTION = "[REDACTED]"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----.*?-----END (?P=kind)-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{12,})")
_GITHUB_TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_URL_TOKEN_RE = re.compile(r"(?i)([?&](?:token|key|access_token|api_key)=)([^\s&\"']+)")
_ASSIGNMENT_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_.-]*\s*[:=]\s*)(['\"]?)([^\r\n'\"]+)(['\"]?)"
)
_INLINE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_.-]*\s*[:=]\s*)(['\"]?)([^\s'\"]+)(['\"]?)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:api[_-]?key|token|secret|password|private[_-]?key|client[_-]?secret)"\s*:\s*")([^"]+)(")'
)


def redact_text(value: str) -> str:
    text = _PRIVATE_KEY_RE.sub("-----BEGIN \\g<kind>-----\n" + REDACTION + "\n-----END \\g<kind>-----", value)
    text = _BEARER_RE.sub("Bearer " + REDACTION, text)
    text = _GITHUB_TOKEN_RE.sub(REDACTION, text)
    text = _GOOGLE_API_KEY_RE.sub(REDACTION, text)
    text = _OPENAI_KEY_RE.sub(REDACTION, text)
    text = _URL_TOKEN_RE.sub(lambda match: f"{match.group(1)}{REDACTION}", text)
    text = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}{match.group(4)}", text)
    text = _INLINE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}{match.group(4)}", text)
    text = _JSON_SECRET_RE.sub(lambda match: f"{match.group(1)}{REDACTION}{match.group(3)}", text)
    return text


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if re.search(r"(?i)(api[_-]?key|token|secret|password|credential|private[_-]?key|client[_-]?secret)", str(key)):
                redacted[key] = REDACTION if item not in (None, "") else item
            else:
                redacted[key] = redact_payload(item)
        return redacted
    return value
