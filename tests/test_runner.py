from __future__ import annotations

import io
import json

from gemness.config import GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import GeminiCliRunner, _StreamJsonState, _record_stream_json_line, _stream_json_stdout, gemness_env


def test_config_defaults_do_not_skip_gemini_trust(monkeypatch) -> None:
    monkeypatch.delenv("GEMNESS_OBSERVER_PORT", raising=False)
    monkeypatch.delenv("GEMNESS_OBSERVER_START_ON_INIT", raising=False)
    monkeypatch.delenv("GEMNESS_GEMINI_OUTPUT_FORMAT", raising=False)
    monkeypatch.delenv("GEMNESS_GEMINI_SKIP_TRUST", raising=False)
    monkeypatch.delenv("GEMNESS_GEMINI_TRUST_WORKSPACE", raising=False)
    monkeypatch.delenv("GEMINI_CLI_TRUST_WORKSPACE", raising=False)

    assert GemnessConfig().gemini_skip_trust is False
    assert GemnessConfig().gemini_trust_workspace is True
    assert GemnessConfig().observer_port == 56755
    assert GemnessConfig().observer_start_on_init is True
    assert GemnessConfig().gemini_output_format == "stream-json"


def test_gemness_env_trusts_workspace_by_default() -> None:
    env = gemness_env(GemnessConfig(), {"HTTPS_PROXY": "http://proxy.local"})

    assert env["HTTPS_PROXY"] == "http://proxy.local"
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_config_can_disable_workspace_trust_with_direct_env(monkeypatch) -> None:
    monkeypatch.delenv("GEMNESS_GEMINI_TRUST_WORKSPACE", raising=False)
    monkeypatch.setenv("GEMINI_CLI_TRUST_WORKSPACE", "false")

    assert GemnessConfig().gemini_trust_workspace is False


def test_runner_missing_command_returns_clear_error(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    runner = GeminiCliRunner(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, gemini_command="definitely-missing-gemini-cli"))

    result = runner.run("hello", model="fake-model", output_format="json", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "error"
    assert "Gemini CLI not found" in result.message


def test_runner_preserves_env_and_uses_cwd_without_default_skip_trust(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeProcess:
        pid = 1234
        stdout = io.StringIO('{"response":"ok"}')
        stderr = io.StringIO("")

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local")
    monkeypatch.setattr("gemness.runner.subprocess.Popen", fake_popen)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    runner = GeminiCliRunner(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, gemini_command="fake-gemini"))

    result = runner.run("hello", model="fake-model", output_format="json", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "completed"
    assert "--skip-trust" not in captured["command"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["HTTPS_PROXY"] == "http://proxy.local"
    assert captured["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_stream_json_lines_emit_live_delta_and_synthesize_json_stdout(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    state = _StreamJsonState()

    _record_stream_json_line(
        '{"type":"message","timestamp":"2026-05-19T03:04:20Z","role":"assistant","content":"안녕","delta":true}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )
    _record_stream_json_line(
        '{"type":"message","timestamp":"2026-05-19T03:04:21Z","role":"assistant","content":"하세요","delta":true}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )
    _record_stream_json_line(
        '{"type":"result","timestamp":"2026-05-19T03:04:22Z","status":"success","stats":{"total_tokens":12}}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )

    events = hub.get_events(session.session_id, raw=True)
    stdout = _stream_json_stdout(state, "")

    assert [event["type"] for event in events].count("gemini.delta") == 2
    assert events[-1]["payload"]["response"] == "안녕하세요"
    assert stdout == '{"response": "안녕하세요", "stats": {"total_tokens": 12}}'


def test_stream_json_tool_use_resets_synthesized_final_response(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    state = _StreamJsonState()

    _record_stream_json_line(
        '{"type":"message","role":"assistant","content":"checking files","delta":true}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )
    _record_stream_json_line(
        '{"type":"tool_use","tool_name":"read_file","tool_id":"t1","parameters":{"path":"a.py"}}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )
    _record_stream_json_line(
        '{"type":"message","role":"assistant","content":"final answer","delta":true}\n',
        state,
        hub=hub,
        session_id=session.session_id,
        phase=None,
    )

    assert json.loads(_stream_json_stdout(state, ""))["response"] == "final answer"
