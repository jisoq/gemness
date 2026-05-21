from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gemness.config import DEFAULT_MODEL_LABEL, GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import AgyCapabilities, AgyRunResult
from gemness.tools import GemnessService
from gemness.workspace import (
    POLICY_AUTOMATIC_CODEX_TRUST,
    POLICY_EXPLICIT_ALLOWED_ROOTS,
    POLICY_NO_POLICY,
    WorkspaceAccessError,
    inspect_workspace_policy,
    resolve_workspace_cwd,
)


class PolicyFakeRunner:
    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def probe_capabilities(self, cwd=None) -> AgyCapabilities:
        return AgyCapabilities(command=["agy"], available=True, resolved="agy", print_flag="-p")

    def run(self, prompt: str, *, session_id: str, hub: ObserverHub, cwd=None, phase=None, **kwargs) -> AgyRunResult:
        self.calls.append({"prompt": prompt, "cwd": cwd})
        hub.set_status(session_id, "running", "antigravity.started", {"model": DEFAULT_MODEL_LABEL, "streaming": False}, role="gemness", phase=phase)
        stdout = json.dumps({"response": self.response, "metadata": {"streaming": False, "run_id": session_id}})
        hub.append_event(session_id, "antigravity.response", "gemness", {"response": stdout, "streaming": False}, phase=phase)
        hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": 0, "streaming": False}, phase=phase)
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})


def test_explicit_allowed_roots_override_codex_trusted_projects(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    explicit = tmp_path / "explicit"
    trusted.mkdir()
    explicit.mkdir()
    _write_codex_projects(tmp_path / "codex", [(trusted, "trusted")], monkeypatch)

    decision = inspect_workspace_policy(GemnessConfig(observer_enabled=False, allowed_roots=(explicit,)), str(trusted))

    assert decision.allowed is False
    assert decision.policy_mode == POLICY_EXPLICIT_ALLOWED_ROOTS
    assert decision.codex_trust_for_cwd == "trusted"
    assert decision.allowed_roots == (explicit.resolve(),)


def test_codex_trusted_project_allows_project_and_children(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    _write_codex_projects(tmp_path / "codex", [(project, "trusted")], monkeypatch)

    config = GemnessConfig(observer_enabled=False)
    decision = inspect_workspace_policy(config, str(child))

    assert resolve_workspace_cwd(config, str(child)) == child.resolve()
    assert decision.allowed is True
    assert decision.allowed_by == "codex_trusted_project"
    assert decision.policy_mode == POLICY_AUTOMATIC_CODEX_TRUST
    assert decision.matched_codex_project == project.resolve()


def test_child_untrusted_codex_project_overrides_parent_trusted_project(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    _write_codex_projects(tmp_path / "codex", [(parent, "trusted"), (child, "untrusted")], monkeypatch)

    with pytest.raises(WorkspaceAccessError) as raised:
        resolve_workspace_cwd(GemnessConfig(observer_enabled=False), str(child))

    payload = raised.value.to_payload()
    assert payload["codex_trust_for_cwd"] == "untrusted"
    assert payload["matched_codex_project"] == str(child.resolve())
    assert "trust_level=untrusted" in payload["message"]


def test_workspace_root_and_codex_trusted_roots_are_both_automatic_roots(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    codex_root = tmp_path / "codex-project"
    workspace_child = workspace_root / "child"
    codex_child = codex_root / "child"
    workspace_child.mkdir(parents=True)
    codex_child.mkdir(parents=True)
    _write_codex_projects(tmp_path / "codex", [(codex_root, "trusted")], monkeypatch)

    config = GemnessConfig(observer_enabled=False, workspace_root=workspace_root)

    workspace_decision = inspect_workspace_policy(config, str(workspace_child))
    codex_decision = inspect_workspace_policy(config, str(codex_child))

    assert workspace_decision.allowed is True
    assert workspace_decision.allowed_by == "workspace_root"
    assert codex_decision.allowed is True
    assert codex_decision.allowed_by == "codex_trusted_project"
    assert {str(root) for root in codex_decision.allowed_roots} == {str(workspace_root.resolve()), str(codex_root.resolve())}


def test_health_reports_missing_or_malformed_codex_config_without_crashing(tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    missing_service = GemnessService(GemnessConfig(transcript_dir=tmp_path / "missing", observer_enabled=False), runner=PolicyFakeRunner())
    try:
        missing = missing_service.antigravity_health(cwd=str(tmp_path), check_antigravity=False)
        assert missing["status"] == "warning"
        assert missing["workspace"]["allowed"] is False
        assert missing["workspace"]["policy_mode"] == POLICY_NO_POLICY
        assert missing["workspace"]["codex_trust_for_cwd"] == "config_missing"
    finally:
        missing_service.shutdown()

    (codex_home / "config.toml").write_text("[projects.\n", encoding="utf-8")
    malformed_service = GemnessService(GemnessConfig(transcript_dir=tmp_path / "malformed", observer_enabled=False), runner=PolicyFakeRunner())
    try:
        malformed = malformed_service.antigravity_health(cwd=str(tmp_path), check_antigravity=False)
        assert malformed["status"] == "warning"
        assert malformed["workspace"]["allowed"] is False
        assert malformed["workspace"]["codex_trust_for_cwd"] == "config_unreadable"
        assert malformed["workspace"]["diagnostics"]
    finally:
        malformed_service.shutdown()


def test_omitted_cwd_uses_workspace_root_or_reports_no_policy(tmp_path, monkeypatch) -> None:
    _write_codex_projects(tmp_path / "codex", [], monkeypatch)
    runner = PolicyFakeRunner()
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path / "transcripts", observer_enabled=False, workspace_root=tmp_path), runner=runner)
    try:
        result = service.ask_antigravity("hello")
        assert result["status"] == "completed"
        assert runner.calls[0]["cwd"] == tmp_path.resolve()
    finally:
        service.shutdown()

    no_policy_runner = PolicyFakeRunner()
    no_policy_service = GemnessService(GemnessConfig(transcript_dir=tmp_path / "no-policy", observer_enabled=False), runner=no_policy_runner)
    try:
        result = no_policy_service.ask_antigravity("hello")
        assert result["status"] == "error"
        assert result["policy_mode"] == POLICY_NO_POLICY
        assert result["codex_trust_for_cwd"] == "absent"
        assert no_policy_runner.calls == []
    finally:
        no_policy_service.shutdown()


def test_untrusted_cwd_fallback_requires_opt_in(tmp_path, monkeypatch) -> None:
    _write_codex_projects(tmp_path / "codex", [], monkeypatch)

    decision = inspect_workspace_policy(GemnessConfig(observer_enabled=False, allow_untrusted_cwd_fallback=True), str(tmp_path))

    assert decision.allowed is True
    assert decision.allowed_by == "untrusted_cwd_fallback"
    assert decision.policy_mode == POLICY_NO_POLICY


def test_untrusted_cwd_fallback_does_not_bypass_explicit_allowed_roots(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    _write_codex_projects(tmp_path / "codex", [], monkeypatch)

    decision = inspect_workspace_policy(
        GemnessConfig(observer_enabled=False, allowed_roots=(allowed,), allow_untrusted_cwd_fallback=True),
        str(outside),
    )

    assert decision.allowed is False
    assert decision.allowed_by is None
    assert decision.policy_mode == POLICY_EXPLICIT_ALLOWED_ROOTS
    assert "outside allowed roots" in str(decision.message)


def test_untrusted_cwd_fallback_does_not_bypass_codex_untrusted_project(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_codex_projects(tmp_path / "codex", [(project, "untrusted")], monkeypatch)

    decision = inspect_workspace_policy(GemnessConfig(observer_enabled=False, allow_untrusted_cwd_fallback=True), str(project))

    assert decision.allowed is False
    assert decision.allowed_by is None
    assert decision.codex_trust_for_cwd == "untrusted"
    assert "trust_level=untrusted" in str(decision.message)


def test_workspace_root_does_not_bypass_child_codex_untrusted_project(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    child = workspace_root / "child"
    child.mkdir(parents=True)
    _write_codex_projects(tmp_path / "codex", [(child, "untrusted")], monkeypatch)

    decision = inspect_workspace_policy(GemnessConfig(observer_enabled=False, workspace_root=workspace_root), str(child))

    assert decision.allowed is False
    assert decision.allowed_by is None
    assert decision.codex_trust_for_cwd == "untrusted"
    assert decision.matched_codex_project == child.resolve()


def _write_codex_projects(codex_home: Path, projects: list[tuple[Path, str | None]], monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for project_path, trust_level in projects:
        lines.append(f"[projects.{json.dumps(str(project_path))}]")
        if trust_level is not None:
            lines.append(f"trust_level = {json.dumps(trust_level)}")
        lines.append("")
    (codex_home / "config.toml").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
