from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

from .mcp_metadata import SERVER_NAME, SERVER_VERSION
from .tools import GemnessService


TOOLS = [
    {
        "name": "health_check",
        "description": "Check MCP server, workspace, observer, and Gemini CLI readiness without calling a model.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cwd": {"type": "string"},
                "check_gemini": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "ask_text",
        "description": "Ask Gemini for advisory text and expose the call in the local observer UI.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "follow_up",
        "description": "Continue a previous Gemini observer conversation from a parent session id.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["parent_session_id", "prompt"],
            "properties": {
                "parent_session_id": {"type": "string"},
                "prompt": {"type": "string"},
                "model": {"type": "string"},
            },
        },
    },
    {
        "name": "ask_json",
        "description": "Ask Gemini for JSON, validate it against a schema, and expose parse/repair events.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prompt", "schema"],
            "properties": {
                "prompt": {"type": "string"},
                "schema": {"type": "object"},
                "model": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "review_current_diff",
        "description": "Review the current git diff with Gemini without granting Gemini shell access.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "base_ref": {"type": "string", "default": "HEAD"},
                "model": {"type": "string"},
                "cwd": {"type": "string"},
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
    if name == "health_check":
        result = service.health_check(cwd=arguments.get("cwd"), check_gemini=bool(arguments.get("check_gemini", True)))
    elif name == "ask_text":
        result = service.ask_text(str(arguments["prompt"]), model=arguments.get("model"), cwd=arguments.get("cwd"))
    elif name == "follow_up":
        result = service.follow_up(str(arguments["parent_session_id"]), str(arguments["prompt"]), model=arguments.get("model"))
    elif name == "ask_json":
        result = service.ask_json(str(arguments["prompt"]), arguments["schema"], model=arguments.get("model"), cwd=arguments.get("cwd"))
    elif name == "review_current_diff":
        result = service.review_current_diff(base_ref=str(arguments.get("base_ref") or "HEAD"), model=arguments.get("model"), cwd=arguments.get("cwd"))
    else:
        raise ValueError(f"Unknown tool: {params.get('name')}")
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "structuredContent": result,
        "isError": result.get("status") == "error",
    }


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
