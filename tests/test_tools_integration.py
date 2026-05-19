from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from gemness.config import GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import GeminiRunResult
from gemness.tools import GemnessService, validate_base_ref


TEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


class FakeRunner:
    def __init__(self, responses: list[str | GeminiRunResult | Callable[..., GeminiRunResult]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        prompt: str,
        *,
        model: str,
        output_format: str,
        session_id: str,
        hub: ObserverHub,
        cwd=None,
        phase: str | None = None,
    ) -> GeminiRunResult:
        self.calls.append({"prompt": prompt, "model": model, "output_format": output_format, "session_id": session_id, "cwd": cwd, "phase": phase})
        hub.set_status(session_id, "running", "gemini.started", {"model": model, "output_format": output_format}, role="gemness", phase=phase)
        response = self.responses.pop(0)
        if callable(response):
            return response(prompt=prompt, model=model, output_format=output_format, session_id=session_id, hub=hub, cwd=cwd, phase=phase)
        if isinstance(response, GeminiRunResult):
            if response.stdout:
                hub.append_event(session_id, "gemini.response", "gemness", {"response": response.stdout}, phase=phase)
            hub.append_event(session_id, "gemini.exited", "gemness", {"exit_code": response.exit_code}, phase=phase)
            return response
        stdout = json.dumps({"response": response})
        hub.append_event(session_id, "gemini.response", "gemness", {"response": stdout}, phase=phase)
        hub.append_event(session_id, "gemini.exited", "gemness", {"exit_code": 0}, phase=phase)
        return GeminiRunResult.completed(stdout)


def make_service(tmp_path, responses, *, pause=False, diff_provider=None, **config_overrides) -> GemnessService:
    config = GemnessConfig(
        model="fake-model",
        transcript_dir=tmp_path,
        observer_enabled=True,
        observer_port=0,
        pause_before_send=pause,
        approval_timeout_sec=2,
        tool_timeout_sec=2,
        **config_overrides,
    )
    return GemnessService(config, runner=FakeRunner(responses), diff_provider=diff_provider)


def test_ask_text_happy_path(tmp_path) -> None:
    service = make_service(tmp_path, ["hello"])
    try:
        result = service.ask_text("Say hello")
        assert result["status"] == "completed"
        assert result["text"] == "hello"
        assert result["session_id"]
        assert result["observer_url"].startswith("http://127.0.0.1:")
        events = service.hub.get_events(result["session_id"], raw=False)
        assert "prompt.sent" in [event["type"] for event in events]
    finally:
        service.shutdown()


def test_ask_json_valid_json(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer":"yes"}'])
    try:
        result = service.ask_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "valid"
        assert result["data"] == {"answer": "yes"}
        assert result["repaired"] is False
        assert result["repair_attempted"] is False
        assert result["repair_succeeded"] is False
    finally:
        service.shutdown()


def test_ask_json_fenced_json(tmp_path) -> None:
    service = make_service(tmp_path, ['```json\n{"answer":"yes"}\n```'])
    try:
        result = service.ask_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "valid"
        assert result["data"] == {"answer": "yes"}
    finally:
        service.shutdown()


def test_ask_json_invalid_then_repair_success(tmp_path) -> None:
    service = make_service(tmp_path, ["not json", '{"answer":"fixed"}'])
    try:
        result = service.ask_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "valid"
        assert result["repaired"] is True
        assert result["repair_attempted"] is True
        assert result["repair_succeeded"] is True
        assert result["data"] == {"answer": "fixed"}
        assert len(service.runner.calls) == 2
    finally:
        service.shutdown()


def test_ask_json_invalid_then_repair_fail_returns_invalid(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer": 7}', '{"answer": 8}'])
    try:
        result = service.ask_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "invalid"
        assert result["repaired"] is False
        assert result["repair_attempted"] is True
        assert result["repair_succeeded"] is False
        assert "validation_errors" in result
        assert len(service.runner.calls) == 2
    finally:
        service.shutdown()


def test_ask_json_invalid_schema_returns_error_before_model_call(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer":"yes"}'])
    try:
        result = service.ask_json("Return answer", {"type": 7})
        assert result["status"] == "error"
        assert result["message"].startswith("Invalid JSON Schema:")
        assert service.runner.calls == []
    finally:
        service.shutdown()


def test_cli_envelope_stats_are_returned(tmp_path) -> None:
    stdout = json.dumps({"response": "hello", "stats": {"tokens": {"total": 12}}})
    service = make_service(tmp_path, [GeminiRunResult.completed(stdout, stats={"runner": "ok"})])
    try:
        result = service.ask_text("Say hello")
        assert result["status"] == "completed"
        assert result["stats"]["runner"] == "ok"
        assert result["stats"]["tokens"]["total"] == 12
    finally:
        service.shutdown()


def test_cli_envelope_error_returns_status_error(tmp_path) -> None:
    stdout = json.dumps({"response": "partial", "error": {"message": "auth failed"}, "stats": {"attempts": 1}})
    service = make_service(tmp_path, [GeminiRunResult.completed(stdout)])
    try:
        result = service.ask_text("Say hello")
        assert result["status"] == "error"
        assert "auth failed" in result["message"]
        assert result["stats"]["attempts"] == 1
    finally:
        service.shutdown()


def test_subprocess_error_returns_status_error(tmp_path) -> None:
    service = make_service(tmp_path, [GeminiRunResult.error("boom", exit_code=2, stderr="bad")])
    try:
        result = service.ask_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "error"
        assert result["exit_code"] == 2
        assert result["stderr_tail"] == "bad"
    finally:
        service.shutdown()


def test_default_workspace_root_is_used_as_runner_cwd(tmp_path) -> None:
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_text("Say hello")
        assert result["status"] == "completed"
        assert service.runner.calls[0]["cwd"] == tmp_path.resolve()
    finally:
        service.shutdown()


def test_requested_cwd_under_allowed_root_is_accepted(tmp_path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_text("Say hello", cwd=str(child))
        assert result["status"] == "completed"
        assert service.runner.calls[0]["cwd"] == child.resolve()
    finally:
        service.shutdown()


def test_requested_cwd_outside_allowed_root_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_text("Say hello", cwd=str(outside))
        assert result["status"] == "error"
        assert "outside allowed roots" in result["message"]
        assert service.runner.calls == []
    finally:
        service.shutdown()


def test_review_current_diff_passes_resolved_cwd_to_diff_provider(tmp_path) -> None:
    review = {
        "verdict": "pass",
        "summary": "No findings.",
        "findings": [],
        "recommended_actions": [],
    }
    captured: dict[str, Any] = {}

    def diff_provider(base_ref, cwd):
        captured["base_ref"] = base_ref
        captured["cwd"] = cwd
        return "diff --git a/a.py b/a.py\n"

    service = make_service(tmp_path / "transcripts", [json.dumps(review)], workspace_root=tmp_path, allowed_roots=(tmp_path,), diff_provider=diff_provider)
    try:
        result = service.review_current_diff("HEAD")
        assert result["status"] == "valid"
        assert captured == {"base_ref": "HEAD", "cwd": tmp_path.resolve()}
        assert service.runner.calls[0]["cwd"] == tmp_path.resolve()
    finally:
        service.shutdown()


def test_review_current_diff_with_fake_diff(tmp_path) -> None:
    review = {
        "verdict": "pass",
        "summary": "No findings.",
        "findings": [],
        "recommended_actions": [],
    }
    service = make_service(tmp_path, [json.dumps(review)], diff_provider=lambda base, cwd: "diff --git a/a.py b/a.py\n")
    try:
        result = service.review_current_diff("HEAD")
        assert result["status"] == "valid"
        assert result["data"]["verdict"] == "pass"
        events = service.hub.get_events(result["session_id"], raw=True)
        assert any("diff --git" in event["payload"].get("prompt", "") for event in events if event["type"] == "prompt.rendered")
    finally:
        service.shutdown()


def test_git_diff_uses_explicit_cwd(tmp_path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(args, 0, stdout="diff", stderr="")

    import subprocess

    monkeypatch.setattr("gemness.tools.subprocess.run", fake_run)
    service = make_service(tmp_path, [])

    assert service._git_diff("HEAD", tmp_path) == "diff"
    assert captured["cwd"] == tmp_path


def test_validate_base_ref_accepts_safe_refs() -> None:
    assert validate_base_ref("HEAD") == "HEAD"
    assert validate_base_ref("origin/main...HEAD") == "origin/main...HEAD"


def test_validate_base_ref_rejects_unsafe_refs() -> None:
    for ref in ["", "--no-index", "HEAD -- path", "HEAD\nnext", "x" * 201]:
        try:
            validate_base_ref(ref)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid base_ref: {ref!r}")


def test_queued_prompt_edit_before_send(tmp_path) -> None:
    service = make_service(tmp_path, ["edited ok"], pause=True)
    holder: dict[str, Any] = {}
    try:
        thread = threading.Thread(target=lambda: holder.setdefault("result", service.ask_text("original")), daemon=True)
        thread.start()
        session_id = _wait_for_session_status(service.hub, "waiting_for_user_approval")
        service.hub.add_intervention(session_id, "edit_prompt", prompt="edited prompt")
        service.hub.add_intervention(session_id, "approve")
        thread.join(timeout=3)

        assert holder["result"]["status"] == "completed"
        assert service.runner.calls[0]["prompt"] == "edited prompt"
    finally:
        service.shutdown()


def test_running_interrupt_and_retry(tmp_path) -> None:
    def interrupt(**kwargs) -> GeminiRunResult:
        hub = kwargs["hub"]
        session_id = kwargs["session_id"]
        hub.add_intervention(session_id, "interrupt_retry", instruction="focus on data loss")
        intervention = hub.consume_running_intervention(session_id)
        return GeminiRunResult.interrupted(intervention.instruction, stdout="partial output")

    service = make_service(tmp_path, [interrupt, "retried answer"])
    try:
        result = service.ask_text("review this")
        assert result["status"] == "completed"
        assert result["text"] == "retried answer"
        assert service.runner.calls[1]["session_id"] != service.runner.calls[0]["session_id"]
        assert "focus on data loss" in service.runner.calls[1]["prompt"]
        parent = service.runner.calls[0]["session_id"]
        child = service.runner.calls[1]["session_id"]
        assert service.hub.get_session(child)["parent_session_id"] == parent
    finally:
        service.shutdown()


def test_completed_follow_up_creates_parent_linked_session(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second"])
    try:
        first = service.ask_text("first prompt")
        second = service.follow_up(first["session_id"], "go deeper")
        assert second["status"] == "completed"
        assert service.hub.get_session(second["session_id"])["parent_session_id"] == first["session_id"]
        assert "go deeper" in service.runner.calls[1]["prompt"]
    finally:
        service.shutdown()


def _wait_for_session_status(hub: ObserverHub, status: str) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for session in hub.sessions.values():
            if session.status == status:
                return session.session_id
        time.sleep(0.02)
    raise AssertionError(f"No session reached status {status}")
