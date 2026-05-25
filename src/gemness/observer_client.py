from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_json(url: str, *, timeout: float = 0.75) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"
    return payload, None


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    timeout: float = 1.0,
) -> tuple[dict[str, Any] | None, str | None, int | None]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gemness-Management-Token"] = token
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else None, None, response.status
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"error": str(exc)}
        return data if isinstance(data, dict) else {"error": str(exc)}, str(exc), exc.code
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return None, str(exc), None
