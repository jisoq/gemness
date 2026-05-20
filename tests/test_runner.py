from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from gemness.config import DEFAULT_MODEL_LABEL, GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import AgyCliRunner, clean_console_output, command_exists, gemness_env, resolve_agy_command


DEFAULT_HELP = """Usage of agy:
  -p                              Short alias for --print
  --print                         Run a single prompt non-interactively and print the response
  --prompt                        Alias for --print
  --continue                      Continue the most recent conversation
  --conversation                  Resume a previous conversation by ID
"""


def test_config_defaults_use_antigravity_env(monkeypatch) -> None:
    monkeypatch.delenv("GEMNESS_AGY_COMMAND", raising=False)
    monkeypatch.delenv("GEMNESS_AGY_TIMEOUT", raising=False)
    monkeypatch.delenv("GEMNESS_AGY_CAPTURE_MODE", raising=False)
    monkeypatch.delenv("GEMNESS_OBSERVER_PORT", raising=False)
    monkeypatch.delenv("GEMNESS_OBSERVER_START_ON_INIT", raising=False)

    config = GemnessConfig()

    assert config.agy_command == "agy"
    assert config.agy_timeout_sec == 600
    assert config.agy_queue_limit == 64
    assert config.agy_capture_mode == "auto"
    assert config.observer_port == 56755
    assert config.observer_start_on_init is True


def test_config_reads_agy_overrides(monkeypatch) -> None:
    monkeypatch.setenv("GEMNESS_AGY_COMMAND", "custom-agy")
    monkeypatch.setenv("GEMNESS_AGY_TIMEOUT", "12")
    monkeypatch.setenv("GEMNESS_AGY_CAPTURE_MODE", "pipe")

    config = GemnessConfig()

    assert config.agy_command == "custom-agy"
    assert config.agy_timeout_sec == 12
    assert config.agy_capture_mode == "pipe"


def test_gemness_env_preserves_existing_values() -> None:
    env = gemness_env(GemnessConfig(), {"HTTPS_PROXY": "http://proxy.local"})

    assert env["HTTPS_PROXY"] == "http://proxy.local"
    assert ("GEM" + "INI_CLI_TRUST_WORKSPACE") not in env


def test_windows_localappdata_fallback_is_discovered(tmp_path, monkeypatch) -> None:
    fallback = tmp_path / "agy" / "bin" / "agy.exe"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", "")

    assert resolve_agy_command("agy") == [str(fallback)]
    assert command_exists("agy") is True


def test_runner_missing_command_returns_clear_error(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL)
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, agy_command="definitely-missing-agy-cli", agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "error"
    assert "Antigravity CLI not found" in result.message


def test_runner_reprobes_after_failed_capability_probe(tmp_path, monkeypatch) -> None:
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, agy_command="agy", agy_capture_mode="pipe"))
    help_calls = 0

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003
        nonlocal help_calls
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, stdout="1.0.0\n", stderr="")
        if args[-1] == "--help":
            help_calls += 1
            if help_calls == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="temporary failure")
            return subprocess.CompletedProcess(args, 0, stdout=DEFAULT_HELP, stderr="")
        raise AssertionError(f"Unexpected probe command: {args!r}")

    monkeypatch.setattr("gemness.runner._executable_exists", lambda executable: True)
    monkeypatch.setattr("gemness.runner.subprocess.run", fake_run)

    failed = runner.probe_capabilities(tmp_path)
    recovered = runner.probe_capabilities(tmp_path)
    cached = runner.probe_capabilities(tmp_path)

    assert failed.available is False
    assert recovered.available is True
    assert recovered.print_flag == "-p"
    assert cached is recovered
    assert help_calls == 2


def test_runner_uses_print_mode_and_never_unsupported_flags(tmp_path) -> None:
    command, record_path = make_fake_agy(tmp_path, stdout="ok")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    unsupported = [
        "--output-" + "format",
        "stream-" + "json",
        "--session-" + "id",
        "--res" + "ume",
        "--approval-" + "mode",
        "--skip-" + "trust",
        "--mo" + "del",
        "--reasoning-" + "effort",
    ]
    assert result.status == "completed"
    assert recorded == ["-p", "hello"]
    assert not any(flag in recorded for flag in unsupported)
    assert hub.get_session(session.session_id)["command_argv"][-2:] == ["-p", "[PROMPT_REDACTED]"]


def test_runner_detaches_agy_stdin(tmp_path, monkeypatch) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="ok")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))
    runner.probe_capabilities(tmp_path)
    original_popen = subprocess.Popen
    captured: dict[str, object] = {}

    def recording_popen(*args, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr("gemness.runner.subprocess.Popen", recording_popen)

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "completed"
    assert captured["stdin"] is subprocess.DEVNULL


def test_runner_synthesizes_non_streaming_envelope_and_metadata(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="final answer", stderr="diagnostic")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)
    envelope = json.loads(result.stdout)

    assert result.status == "completed"
    assert envelope["response"] == "final answer\n"
    assert envelope["metadata"]["run_id"] == session.session_id
    assert envelope["metadata"]["conversation_id"] == session.conversation_id
    assert envelope["metadata"]["cwd"] == str(tmp_path)
    assert envelope["metadata"]["command"][-2:] == ["-p", "[PROMPT_REDACTED]"]
    assert envelope["metadata"]["exit_code"] == 0
    assert envelope["metadata"]["auth_status"] == "ok"
    assert envelope["metadata"]["streaming"] is False
    assert result.stderr == "diagnostic\n"
    events = hub.get_events(session.session_id, raw=True)
    response_event = next(event for event in events if event["type"] == "antigravity.response")
    assert "antigravity.started" in [event["type"] for event in events]
    assert "antigravity.response" in [event["type"] for event in events]
    assert "antigravity.stderr" in [event["type"] for event in events]
    assert "antigravity.exited" in [event["type"] for event in events]
    assert response_event["payload"]["response_preview"] == "final answer\n"
    assert "response" not in response_event["payload"]
    assert "stdout" not in response_event["payload"]
    artifact_path = Path(response_event["payload"]["stdout_artifact"]["path"])
    assert artifact_path.read_text(encoding="utf-8") == "final answer\n"


def test_runner_reports_auth_required_from_stderr(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="", stderr="You are not logged in to Antigravity", exit_code=1)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "error"
    assert result.metadata["auth_status"] == "auth_required"
    assert "authentication is required" in result.message


def test_runner_does_not_treat_auth_words_in_success_stdout_as_auth_required(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="This answer discusses authorization and login flows.")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("explain auth", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "completed"
    assert result.metadata["auth_status"] == "ok"


def test_runner_reports_empty_success_output_as_error(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="", exit_code=0)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "error"
    assert result.message == "Antigravity CLI returned no output"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["auth_status"] == "unknown"


def test_console_capture_output_is_cleaned() -> None:
    value = "\x1b[1t\x1b[c\x1b[?1004h\x1b]0;agy.exe\x1b\\hello\r\nworld\r\n"

    assert clean_console_output(value) == "hello\nworld"


def test_runner_uses_print_alias_from_help_when_short_flag_is_missing(tmp_path) -> None:
    help_text = "Usage of agy:\n  --print   Run a single prompt non-interactively\n"
    command, record_path = make_fake_agy(tmp_path, stdout="ok", help_text=help_text)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL)
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    assert result.status == "completed"
    assert json.loads(record_path.read_text(encoding="utf-8")) == ["--print", "hello"]


def test_runner_uses_native_conversation_flag_when_requested(tmp_path) -> None:
    native_id = "9b68b5b2-b7f5-4a82-9aa3-8fcd2e831517"
    command, record_path = make_fake_agy(tmp_path, stdout="ok")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL)
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("follow up", session_id=session.session_id, hub=hub, cwd=tmp_path, native_conversation_id=native_id)

    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    envelope = json.loads(result.stdout)
    assert result.status == "completed"
    assert recorded == ["--conversation", native_id, "-p", "follow up"]
    assert envelope["metadata"]["native_session_mode"] == "conversation"
    assert envelope["metadata"]["agy_conversation_id"] == native_id
    assert envelope["metadata"]["command"][-2:] == ["-p", "[PROMPT_REDACTED]"]
    assert hub.get_session(session.session_id, raw=True)["agy_conversation_id"] == native_id


def test_runner_does_not_use_native_continue_without_conversation_id(tmp_path) -> None:
    command, record_path = make_fake_agy(tmp_path, stdout="ok")
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL)
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("follow up", session_id=session.session_id, hub=hub, cwd=tmp_path)

    envelope = json.loads(result.stdout)
    assert result.status == "completed"
    assert json.loads(record_path.read_text(encoding="utf-8")) == ["-p", "follow up"]
    assert envelope["metadata"]["native_session_mode"] == "new"
    assert envelope["metadata"]["agy_conversation_id"] is None


def test_runner_does_not_infer_native_conversation_id_from_conversation_file(tmp_path) -> None:
    native_id = "9b68b5b2-b7f5-4a82-9aa3-8fcd2e831517"
    conversation_dir = tmp_path / "agy-conversations"
    command, _record_path = make_fake_agy(tmp_path, stdout="ok", conversation_dir=conversation_dir, conversation_id=native_id)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL)
    runner = AgyCliRunner(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, agy_command=command, agy_timeout_sec=5, agy_capture_mode="pipe"))

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path)

    envelope = json.loads(result.stdout)
    raw_session = hub.get_session(session.session_id, raw=True)
    raw_conversation = hub.get_conversation(session.conversation_id, raw=True)
    assert result.status == "completed"
    assert (conversation_dir / f"{native_id}.pb").exists()
    assert envelope["metadata"]["agy_conversation_id"] is None
    assert raw_session["agy_conversation_id"] != native_id
    assert raw_conversation["current_agy_conversation_id"] != native_id


def test_runner_emits_heartbeat_payload_while_process_is_running(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="ok", sleep_sec=0.3)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=False,
            agy_command=command,
            agy_timeout_sec=5,
            agy_capture_mode="pipe",
            agy_heartbeat_interval_sec=0.1,
        )
    )
    heartbeats: list[dict[str, object]] = []

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path, heartbeat_callback=heartbeats.append)

    assert result.status == "completed"
    assert heartbeats
    assert heartbeats[0]["pid"]
    assert heartbeats[0]["capture_mode"] == "pipe"
    assert "timeout_remaining_ms" in heartbeats[0]


def test_runner_heartbeat_reports_stdout_bytes_before_exit(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="late", stdout_before_sleep="early", sleep_sec=0.3)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=False,
            agy_command=command,
            agy_timeout_sec=5,
            agy_capture_mode="pipe",
            agy_heartbeat_interval_sec=0.05,
        )
    )
    heartbeats: list[dict[str, object]] = []

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path, heartbeat_callback=heartbeats.append)

    assert result.status == "completed"
    assert any(int(heartbeat["stdout_bytes"]) >= len("early\n".encode("utf-8")) for heartbeat in heartbeats)


def test_runner_reports_cancelled_when_manager_terminates_process(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="late", sleep_sec=2)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=False,
            agy_command=command,
            agy_timeout_sec=5,
            agy_capture_mode="pipe",
        )
    )
    cancel_event = threading.Event()

    def terminate_after_start(running) -> None:  # noqa: ANN001
        cancel_event.set()
        running.terminate()

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path, cancel_event=cancel_event, process_callback=terminate_after_start)

    assert result.status == "cancelled"
    assert result.metadata["cancelled"] is True


def test_runner_preserves_completed_output_when_cancel_arrives_after_process_exit(tmp_path) -> None:
    command, _record_path = make_fake_agy(tmp_path, stdout="done", sleep_sec=0)
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False))
    session = hub.create_session("ask_antigravity", DEFAULT_MODEL_LABEL, project_root=str(tmp_path))
    runner = AgyCliRunner(
        GemnessConfig(
            transcript_dir=tmp_path / "transcripts",
            observer_enabled=False,
            agy_command=command,
            agy_timeout_sec=5,
            agy_capture_mode="pipe",
        )
    )
    cancel_event = threading.Event()

    def mark_cancel_after_exit(running) -> None:  # noqa: ANN001
        deadline = time.monotonic() + 2
        while running.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_event.set()

    result = runner.run("hello", session_id=session.session_id, hub=hub, cwd=tmp_path, cancel_event=cancel_event, process_callback=mark_cancel_after_exit)

    envelope = json.loads(result.stdout)
    assert result.status == "completed"
    assert envelope["response"] == "done\n"
    assert "cancelled" not in result.metadata


def make_fake_agy(
    tmp_path: Path,
    *,
    stdout: str,
    stderr: str = "",
    exit_code: int = 0,
    help_text: str = DEFAULT_HELP,
    conversation_dir: Path | None = None,
    conversation_id: str | None = None,
    sleep_sec: float = 0,
    stdout_before_sleep: str = "",
) -> tuple[str, Path]:
    record_path = tmp_path / "agy-argv.json"
    script_path = tmp_path / "fake_agy.py"
    script_path.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json, sys, time",
                "from pathlib import Path",
                f"record_path = {str(record_path)!r}",
                f"help_text = {help_text!r}",
                f"stdout = {stdout!r}",
                f"stdout_before_sleep = {stdout_before_sleep!r}",
                f"stderr = {stderr!r}",
                f"exit_code = {exit_code!r}",
                f"conversation_dir = {None if conversation_dir is None else str(conversation_dir)!r}",
                f"conversation_id = {conversation_id!r}",
                f"sleep_sec = {sleep_sec!r}",
                "args = sys.argv[1:]",
                "if args == ['--help']:",
                "    print(help_text)",
                "    raise SystemExit(0)",
                "if args == ['--version']:",
                "    print('1.0.0')",
                "    raise SystemExit(0)",
                "with open(record_path, 'w', encoding='utf-8') as handle:",
                "    json.dump(args, handle)",
                "if conversation_dir and conversation_id:",
                "    Path(conversation_dir).mkdir(parents=True, exist_ok=True)",
                "    path = Path(conversation_dir) / f'{conversation_id}.pb'",
                "    path.write_bytes(path.read_bytes() + b'x' if path.exists() else b'x')",
                "if stdout_before_sleep:",
                "    print(stdout_before_sleep, flush=True)",
                "if sleep_sec:",
                "    time.sleep(sleep_sec)",
                "if stdout:",
                "    print(stdout)",
                "if stderr:",
                "    print(stderr, file=sys.stderr)",
                "raise SystemExit(exit_code)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = tmp_path / "agy.cmd"
        wrapper.write_text(f"@echo off\r\n\"{sys.executable}\" \"{script_path}\" %*\r\n", encoding="utf-8")
        return str(wrapper), record_path
    script_path.write_text(f"#!{sys.executable}\n" + script_path.read_text(encoding="utf-8"), encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return str(script_path), record_path
