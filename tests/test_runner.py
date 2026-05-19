from __future__ import annotations

import io

from gemness.config import GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import GeminiCliRunner, gemness_env


def test_config_defaults_do_not_skip_gemini_trust() -> None:
    assert GemnessConfig().gemini_skip_trust is False
    assert GemnessConfig().gemini_trust_workspace is True


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
