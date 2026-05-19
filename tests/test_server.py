from __future__ import annotations

import json

from gemness.config import GemnessConfig
from gemness.runner import GeminiRunResult
from gemness.server import _handle_message
from gemness.tools import GemnessService


class ServerFakeRunner:
    def run(self, prompt, *, model, output_format, session_id, hub, cwd=None, phase=None):
        hub.set_status(session_id, "running", "gemini.started", {"model": model}, role="gemness", phase=phase)
        stdout = json.dumps({"response": "server ok"})
        hub.append_event(session_id, "gemini.response", "gemness", {"response": stdout}, phase=phase)
        hub.append_event(session_id, "gemini.exited", "gemness", {"exit_code": 0}, phase=phase)
        return GeminiRunResult.completed(stdout)


def test_server_tools_list_and_call(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0), runner=ServerFakeRunner())
    try:
        listed = _handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert "health_check" in names
        assert "ask_text" in names
        assert "ask_json" in names
        assert "review_current_diff" in names

        called = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ask_text", "arguments": {"prompt": "hello"}},
            },
            service,
        )
        result = called["result"]["structuredContent"]
        assert result["status"] == "completed"
        assert result["text"] == "server ok"
        assert result["observer_url"].startswith("http://127.0.0.1:")
    finally:
        service.shutdown()


def test_health_check_tool_returns_structured_result(tmp_path) -> None:
    service = GemnessService(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=True,
            observer_port=0,
            workspace_root=tmp_path,
            allowed_roots=(tmp_path,),
            gemini_command="definitely-missing-gemini-cli",
        ),
        runner=ServerFakeRunner(),
    )
    try:
        called = _handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "health_check", "arguments": {"check_gemini": True}},
            },
            service,
        )
        result = called["result"]["structuredContent"]
        assert result["status"] == "warning"
        assert result["server"]["name"] == "gemness"
        assert "health_check" in result["mcp"]["tools"]
        assert result["workspace"]["cwd"] == str(tmp_path.resolve())
        assert result["gemini"]["available"] is False
        assert result["gemini"]["trust_workspace"] is True
        assert any("not found" in warning for warning in result["warnings"])
        assert "token=" not in result["observer"]["url"]
    finally:
        service.shutdown()
