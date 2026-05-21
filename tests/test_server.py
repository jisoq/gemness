from __future__ import annotations

import io
import json
from urllib.parse import urlparse
from urllib.request import urlopen

from jsonschema import Draft7Validator

from gemness.config import DEFAULT_MODEL_LABEL, GemnessConfig
from gemness.mcp_metadata import TOOL_NAMES
from gemness.runner import AgyRunResult
from gemness.server import TOOLS, _handle_message, _read_message, _write_message
from gemness.tools import GemnessService


class ServerFakeRunner:
    def run(self, prompt, *, session_id, hub, cwd=None, phase=None, **kwargs):
        hub.set_status(session_id, "running", "antigravity.started", {"model": DEFAULT_MODEL_LABEL, "streaming": False}, role="gemness", phase=phase)
        stdout = json.dumps({"response": "server ok", "metadata": {"streaming": False, "run_id": session_id}})
        hub.append_event(session_id, "antigravity.response", "gemness", {"response": stdout, "streaming": False}, phase=phase)
        hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": 0, "streaming": False}, phase=phase)
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})


def test_service_starts_observer_before_first_tool_call(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path), runner=ServerFakeRunner())
    try:
        assert service.hub.web_server_running
        with urlopen(f"{service.hub.base_url}/", timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "Gemness 관찰자" in html
    finally:
        service.shutdown()


def test_service_can_defer_observer_until_antigravity_probe(tmp_path) -> None:
    service = GemnessService(
        GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, observer_start_on_init=False, workspace_root=tmp_path),
        runner=ServerFakeRunner(),
    )
    try:
        assert not service.hub.web_server_running
        result = service.antigravity_health(check_antigravity=False)
        assert result["observer"]["running"] is True
        assert result["observer"]["url"].startswith("http://127.0.0.1:")
        assert service.hub.web_server_running
    finally:
        service.shutdown()


def test_second_observer_on_same_port_reuses_existing_dashboard_url(tmp_path) -> None:
    first = GemnessService(GemnessConfig(transcript_dir=tmp_path / "one", observer_enabled=True, observer_port=0, workspace_root=tmp_path), runner=ServerFakeRunner())
    second = None
    try:
        port = urlparse(first.hub.base_url).port
        assert port is not None
        second = GemnessService(
            GemnessConfig(transcript_dir=tmp_path / "two", observer_enabled=True, observer_port=port, workspace_root=tmp_path),
            runner=ServerFakeRunner(),
        )

        assert second.hub.web_server_running is False
        assert second.hub.base_url == f"http://127.0.0.1:{port}"
        result = second.ask_antigravity("hello from second")
        assert result["observer_url"] == f"http://127.0.0.1:{port}/"
    finally:
        if second is not None:
            second.shutdown()
        first.shutdown()


def test_server_tools_list_and_call(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path), runner=ServerFakeRunner())
    try:
        listed = _handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert names == TOOL_NAMES
        assert "start_follow_up_antigravity" not in names
        assert "start_antigravity_json" not in names
        assert "start_review_current_diff_with_antigravity" not in names
        assert "get_antigravity_run" not in names

        called = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ask_antigravity", "arguments": {"prompt": "hello"}},
            },
            service,
        )
        result = called["result"]["structuredContent"]
        assert result["status"] == "completed"
        assert result["text"] == "server ok"
        assert result["observer_url"].startswith("http://127.0.0.1:")

        followed = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "follow_up_antigravity", "arguments": {"parent_session_id": result["session_id"], "prompt": "continue"}},
            },
            service,
        )
        follow_up_result = followed["result"]["structuredContent"]
        assert follow_up_result["status"] == "completed"
        assert follow_up_result["conversation_id"] == result["conversation_id"]

        started = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"prompt": "detached"}},
            },
            service,
        )
        start_result = started["result"]["structuredContent"]
        assert start_result["status"] == "accepted"

        awaited = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "await_antigravity_run", "arguments": {"run_id": start_result["run_id"], "timeout_sec": 2}},
            },
            service,
        )
        await_result = awaited["result"]["structuredContent"]
        assert await_result["status"] == "completed"
        assert await_result["result"]["text"] == "server ok"

        started_json = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "start_antigravity",
                    "arguments": {"mode": "json", "prompt": "detached json", "schema": {"type": "object"}},
                },
            },
            service,
        )
        started_json_result = started_json["result"]["structuredContent"]
        assert started_json_result["status"] == "accepted"
        awaited_json = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "await_antigravity_run", "arguments": {"run_id": started_json_result["run_id"], "timeout_sec": 2}},
            },
            service,
        )
        assert awaited_json["result"]["structuredContent"]["status"] == "invalid"

        legacy_status = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "get_antigravity_run", "arguments": {"run_id": start_result["run_id"]}},
            },
            service,
        )
        assert legacy_status["result"]["structuredContent"]["status"] == "completed"
    finally:
        service.shutdown()


def test_start_antigravity_schema_declares_mode_requirements() -> None:
    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == "start_antigravity")
    validator = Draft7Validator(schema)

    assert not validator.is_valid({})
    assert not validator.is_valid({"mode": "ask"})
    assert validator.is_valid({"prompt": "hello"})
    assert validator.is_valid({"mode": "ask", "prompt": "hello"})
    assert not validator.is_valid({"prompt": "hello", "schema": {"type": "object"}})
    assert not validator.is_valid({"mode": "ask", "prompt": "hello", "schema": {"type": "object"}})
    assert not validator.is_valid({"mode": "json", "prompt": "hello"})
    assert validator.is_valid({"mode": "json", "prompt": "hello", "schema": {"type": "object"}})
    assert validator.is_valid({"mode": "review_current_diff"})
    assert not validator.is_valid({"mode": "follow_up", "prompt": "continue"})
    assert validator.is_valid({"mode": "follow_up", "parent_session_id": "session-1", "prompt": "continue"})


def test_start_antigravity_consolidated_modes_route_to_detached_runs(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path), runner=ServerFakeRunner())
    try:
        parent = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ask_antigravity", "arguments": {"prompt": "parent"}},
            },
            service,
        )["result"]["structuredContent"]

        review_started = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"mode": "review_current_diff", "base_ref": "HEAD"}},
            },
            service,
        )["result"]["structuredContent"]
        assert review_started["status"] == "accepted"
        assert service.hub.get_session(review_started["run_id"])["tool_name"] == "review_current_diff_with_antigravity"

        review_done = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "await_antigravity_run", "arguments": {"run_id": review_started["run_id"], "timeout_sec": 2}},
            },
            service,
        )["result"]["structuredContent"]
        assert review_done["status"] == "invalid"

        follow_started = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "start_antigravity",
                    "arguments": {"mode": "follow_up", "parent_session_id": parent["session_id"], "prompt": "continue"},
                },
            },
            service,
        )["result"]["structuredContent"]
        assert follow_started["status"] == "accepted"

        follow_done = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "await_antigravity_run", "arguments": {"run_id": follow_started["run_id"], "timeout_sec": 2}},
            },
            service,
        )["result"]["structuredContent"]
        assert follow_done["status"] == "completed"
        assert follow_done["result"]["conversation_id"] == parent["conversation_id"]
    finally:
        service.shutdown()


def test_start_antigravity_mode_validation_errors(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, workspace_root=tmp_path), runner=ServerFakeRunner())
    try:
        missing_schema = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"mode": "json", "prompt": "json please"}},
            },
            service,
        )
        assert "schema is required" in missing_schema["error"]["message"]

        schema_without_json_mode = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"prompt": "json please", "schema": {"type": "object"}}},
            },
            service,
        )
        assert "schema requires start_antigravity mode 'json'" in schema_without_json_mode["error"]["message"]

        invalid_mode = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"mode": "unknown", "prompt": "hello"}},
            },
            service,
        )
        assert "Unknown start_antigravity mode" in invalid_mode["error"]["message"]

        non_string_prompt = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"mode": "ask", "prompt": 7}},
            },
            service,
        )
        assert "prompt must be a string" in non_string_prompt["error"]["message"]

        missing_parent = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "start_antigravity", "arguments": {"mode": "follow_up", "prompt": "continue"}},
            },
            service,
        )
        assert "parent_session_id is required" in missing_parent["error"]["message"]
    finally:
        service.shutdown()


def test_server_stdio_uses_json_lines() -> None:
    incoming = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')

    assert _read_message(incoming) == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    outgoing = io.BytesIO()
    _write_message(outgoing, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    payload = outgoing.getvalue()
    assert payload.endswith(b"\n")
    assert b"Content-Length" not in payload
    assert json.loads(payload.decode("utf-8"))["result"] == {"ok": True}


def test_server_still_reads_content_length_for_legacy_smoke_clients() -> None:
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    incoming = io.BytesIO(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)

    assert _read_message(incoming) == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_server_empty_resources_and_prompts(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, workspace_root=tmp_path))
    try:
        resources = _handle_message({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, service)
        prompts = _handle_message({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}, service)

        assert resources["result"] == {"resources": []}
        assert prompts["result"] == {"prompts": []}
    finally:
        service.shutdown()


def test_health_tool_returns_structured_antigravity_result(tmp_path) -> None:
    service = GemnessService(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=True,
            observer_port=0,
            workspace_root=tmp_path,
            allowed_roots=(tmp_path,),
            agy_command="definitely-missing-agy-cli",
        ),
        runner=ServerFakeRunner(),
    )
    try:
        called = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "antigravity_health", "arguments": {"check_antigravity": True}},
            },
            service,
        )
        result = called["result"]["structuredContent"]
        assert result["status"] == "warning"
        assert result["server"]["name"] == "gemness"
        assert "antigravity_health" in result["mcp"]["tools"]
        assert result["workspace"]["cwd"] == str(tmp_path.resolve())
        assert result["antigravity"]["available"] is False
        assert result["antigravity"]["streaming"] is False
        assert any("not found" in warning for warning in result["warnings"])
        assert "token=" not in result["observer"]["url"]
        assert result["observer"]["start_on_init"] is True
        assert result["observer"]["running"] is True
    finally:
        service.shutdown()
