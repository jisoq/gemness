from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, BinaryIO

from .mcp_metadata import TOOL_NAMES


def run_smoke(
    command: list[str],
    *,
    real: bool = False,
    timeout: float = 10.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd or Path.cwd(),
        env=env or os.environ.copy(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    lines: list[str] = []
    try:
        initialize = _request(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout)
        server_info = initialize["result"]["serverInfo"]
        lines.append(f"initialize ok: {server_info['name']} {server_info['version']}")

        _write_message(process.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        listed = _request(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, timeout)
        tools = {tool["name"] for tool in listed["result"]["tools"]}
        missing = sorted(set(TOOL_NAMES) - tools)
        if missing:
            raise RuntimeError(f"Missing tools: {missing}")
        lines.append(f"tools/list ok: {', '.join(sorted(tools))}")

        health = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "health_check", "arguments": {"check_gemini": False}},
            },
            timeout,
        )
        health_result = health["result"]["structuredContent"]
        if health_result["status"] not in {"ok", "warning"}:
            raise RuntimeError(f"health_check failed: {health_result}")
        lines.append(f"health_check ok: status={health_result['status']} cwd={health_result['workspace']['cwd']}")

        if real:
            asked = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "ask_text",
                        "arguments": {"prompt": "Reply with one short sentence confirming Gemness smoke test connectivity."},
                    },
                },
                timeout,
            )
            asked_result = asked["result"]["structuredContent"]
            if asked_result["status"] == "error":
                raise RuntimeError(f"ask_text failed: {asked_result}")
            lines.append(f"ask_text ok: status={asked_result['status']}")

        lines.append("MCP smoke test passed")
        return lines
    finally:
        _stop_process(process)


def _request(process: subprocess.Popen[bytes], message: dict[str, Any], timeout: float) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    _write_message(process.stdin, message)
    response = _read_message(process.stdout, timeout)
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def _write_message(stdin: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stdin.write(body)
    stdin.flush()


def _read_message(stdout: BinaryIO, timeout: float) -> dict[str, Any]:
    output: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            headers: dict[str, str] = {}
            while True:
                line = stdout.readline()
                if line == b"":
                    raise RuntimeError("MCP server closed stdout")
                if line in {b"\r\n", b"\n"}:
                    break
                name, _, value = line.decode("ascii").partition(":")
                headers[name.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = stdout.read(length)
            output.put(json.loads(body.decode("utf-8")))
        except BaseException as exc:  # noqa: BLE001 - surface smoke-test failures directly.
            output.put(exc)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        item = output.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("Timed out waiting for MCP response") from exc
    if isinstance(item, BaseException):
        raise item
    return item


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
