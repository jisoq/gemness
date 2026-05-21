from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

from .mcp_metadata import SERVER_NAME, SERVER_VERSION
from .tools import GemnessService


TOOLS = [
    {
        "name": "antigravity_health",
        "description": "Check MCP server, workspace, observer, and Antigravity CLI readiness.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cwd": {"type": "string"},
                "check_antigravity": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "ask_antigravity",
        "description": "Blocking final-result Antigravity advisory tool. Intended for an antigravity reviewer subagent in the default Codex flow; returns a cleaned final result and observer URL, not raw transcript.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "start_antigravity",
        "description": "Start a background Antigravity run and return immediately with a run id. This is the default reviewer-subagent flow; use mode=ask, json, review_current_diff, or follow_up.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "oneOf": [
                {"required": ["prompt"], "not": {"required": ["schema"]}, "properties": {"mode": {"enum": ["ask"]}}},
                {"required": ["mode", "prompt", "schema"], "properties": {"mode": {"const": "json"}}},
                {"required": ["mode"], "properties": {"mode": {"const": "review_current_diff"}}},
                {"required": ["mode", "parent_session_id", "prompt"], "properties": {"mode": {"const": "follow_up"}}},
            ],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["ask", "json", "review_current_diff", "follow_up"],
                    "default": "ask",
                    "description": "Run type. ask uses prompt, json uses prompt+schema, review_current_diff uses base_ref, and follow_up uses parent_session_id+prompt.",
                },
                "prompt": {"type": "string"},
                "schema": {"type": "object"},
                "base_ref": {"type": "string", "default": "HEAD"},
                "parent_session_id": {"type": "string"},
                "cwd": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "follow_up_antigravity",
        "description": "Blocking final-result follow-up for a previous Antigravity observer conversation. Intended for reviewer subagents in the default Codex flow.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["parent_session_id", "prompt"],
            "properties": {
                "parent_session_id": {"type": "string"},
                "prompt": {"type": "string"},
            },
        },
    },
    {
        "name": "ask_antigravity_json",
        "description": "Blocking final-result Antigravity JSON tool. Validates the final response against a schema and returns data plus observer URL without raw transcript.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prompt", "schema"],
            "properties": {
                "prompt": {"type": "string"},
                "schema": {"type": "object"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "review_current_diff_with_antigravity",
        "description": "Blocking final-result current-diff review. Intended for an antigravity reviewer subagent; Antigravity inspects the workspace itself and Gemness returns cleaned advisory data plus observer URL.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "base_ref": {"type": "string", "default": "HEAD"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "await_antigravity_run",
        "description": "Wait briefly for a background Antigravity run, then return completion or the current running state. Use timeout_sec=0 to poll without waiting.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["run_id"],
            "properties": {
                "run_id": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 5, "minimum": 0, "maximum": 30},
                "event_cursor": {"type": "string"},
                "recent_event_limit": {"type": "integer", "default": 20, "minimum": 0, "maximum": 100},
            },
        },
    },
    {
        "name": "cancel_antigravity_run",
        "description": "Advanced background API: request cancellation for a detached Antigravity run by run id.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["run_id"],
            "properties": {
                "run_id": {"type": "string"},
            },
        },
    },
]


def main() -> None:
    service = GemnessService()
    try:
        _serve(sys.stdin.buffer, sys.stdout.buffer, service)
    finally:
        service.shutdown()


def _serve(stdin: BinaryIO, stdout: BinaryIO, service: GemnessService) -> None:
    while True:
        message = _read_message(stdin)
        if message is None:
            return
        response = _handle_message(message, service)
        if response is not None:
            _write_message(stdout, response)


def _handle_message(message: dict[str, Any], service: GemnessService) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            params = message.get("params") or {}
            result = {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            result = _call_tool(message.get("params") or {}, service)
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:  # noqa: BLE001 - MCP server must report tool errors as JSON-RPC errors.
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}


def _call_tool(params: dict[str, Any], service: GemnessService) -> dict[str, Any]:
    name = _normalize_tool_name(params.get("name"))
    arguments = params.get("arguments") or {}
    if name == "antigravity_health":
        result = service.antigravity_health(cwd=arguments.get("cwd"), check_antigravity=bool(arguments.get("check_antigravity", True)))
    elif name == "ask_antigravity":
        result = service.ask_antigravity(str(arguments["prompt"]), cwd=arguments.get("cwd"))
    elif name == "start_antigravity":
        result = _call_start_antigravity(service, arguments)
    elif name == "follow_up_antigravity":
        result = service.follow_up_antigravity(str(arguments["parent_session_id"]), str(arguments["prompt"]))
    elif name == "start_follow_up_antigravity":
        result = service.start_follow_up_antigravity(
            str(arguments["parent_session_id"]),
            str(arguments["prompt"]),
            idempotency_key=arguments.get("idempotency_key"),
        )
    elif name == "ask_antigravity_json":
        result = service.ask_antigravity_json(str(arguments["prompt"]), arguments["schema"], cwd=arguments.get("cwd"))
    elif name == "start_antigravity_json":
        result = service.start_antigravity_json(
            str(arguments["prompt"]),
            arguments["schema"],
            cwd=arguments.get("cwd"),
            idempotency_key=arguments.get("idempotency_key"),
        )
    elif name == "review_current_diff_with_antigravity":
        result = service.review_current_diff_with_antigravity(base_ref=str(arguments.get("base_ref") or "HEAD"), cwd=arguments.get("cwd"))
    elif name == "start_review_current_diff_with_antigravity":
        result = service.start_review_current_diff_with_antigravity(
            base_ref=str(arguments.get("base_ref") or "HEAD"),
            cwd=arguments.get("cwd"),
            idempotency_key=arguments.get("idempotency_key"),
        )
    elif name == "get_antigravity_run":
        result = service.get_antigravity_run(
            str(arguments["run_id"]),
            event_cursor=arguments.get("event_cursor"),
            recent_event_limit=int(arguments.get("recent_event_limit", 20)),
        )
    elif name == "await_antigravity_run":
        result = service.await_antigravity_run(
            str(arguments["run_id"]),
            timeout_sec=float(arguments.get("timeout_sec", 5)),
            event_cursor=arguments.get("event_cursor"),
            recent_event_limit=int(arguments.get("recent_event_limit", 20)),
        )
    elif name == "cancel_antigravity_run":
        result = service.cancel_antigravity_run(str(arguments["run_id"]))
    else:
        raise ValueError(f"Unknown tool: {params.get('name')}")
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "structuredContent": result,
        "isError": result.get("status") == "error",
    }


def _call_start_antigravity(service: GemnessService, arguments: dict[str, Any]) -> dict[str, Any]:
    mode = str(arguments.get("mode") or "ask").strip().lower()
    idempotency_key = arguments.get("idempotency_key")
    if mode in {"ask", "text", "advisory"}:
        if "schema" in arguments:
            raise ValueError("schema requires start_antigravity mode 'json'")
        return service.start_antigravity(
            _required_string(arguments, "prompt", mode=mode),
            cwd=arguments.get("cwd"),
            idempotency_key=idempotency_key,
        )
    if mode == "json":
        if "schema" not in arguments:
            raise ValueError("schema is required when start_antigravity mode is 'json'")
        return service.start_antigravity_json(
            _required_string(arguments, "prompt", mode=mode),
            arguments["schema"],
            cwd=arguments.get("cwd"),
            idempotency_key=idempotency_key,
        )
    if mode in {"review", "review_current_diff"}:
        return service.start_review_current_diff_with_antigravity(
            base_ref=str(arguments.get("base_ref") or "HEAD"),
            cwd=arguments.get("cwd"),
            idempotency_key=idempotency_key,
        )
    if mode in {"follow_up", "follow-up"}:
        return service.start_follow_up_antigravity(
            _required_string(arguments, "parent_session_id", mode=mode),
            _required_string(arguments, "prompt", mode=mode),
            idempotency_key=idempotency_key,
        )
    raise ValueError(f"Unknown start_antigravity mode: {arguments.get('mode')}")


def _required_string(arguments: dict[str, Any], name: str, *, mode: str) -> str:
    if name not in arguments:
        raise ValueError(f"{name} is required when start_antigravity mode is {mode!r}")
    value = arguments[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string when start_antigravity mode is {mode!r}")
    return value


def _normalize_tool_name(name: Any) -> str:
    value = str(name)
    if value.startswith("gemness."):
        return value.split(".", 1)[1]
    return value


def _read_message(stdin: BinaryIO) -> dict[str, Any] | None:
    while True:
        line = stdin.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            continue
        if line.lower().startswith(b"content-length:"):
            return _read_content_length_message(stdin, line)
        return json.loads(line.decode("utf-8"))


def _read_content_length_message(stdin: BinaryIO, first_header: bytes) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    line = first_header
    while True:
        name, _, value = line.decode("ascii").partition(":")
        headers[name.lower()] = value.strip()
        line = stdin.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stdin.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(stdout: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stdout.write(body)
    stdout.write(b"\n")
    stdout.flush()


if __name__ == "__main__":
    main()
