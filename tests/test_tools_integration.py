from __future__ import annotations

import json
import time
from typing import Any, Callable

from gemness.config import DEFAULT_MODEL_LABEL, GemnessConfig
from gemness.observer import ObserverHub
from gemness.runner import AgyCapabilities, AgyRunResult
from gemness.tools import GemnessService, validate_base_ref


TEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
FENCED_DIFF_MARKER = "```" + "diff"
PATCH_HEADER_MARKER = "diff " + "--git"


class FakeRunner:
    def __init__(
        self,
        responses: list[str | AgyRunResult | Callable[..., AgyRunResult]],
        *,
        supports_continue: bool = True,
        supports_conversation: bool = True,
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.supports_continue = supports_continue
        self.supports_conversation = supports_conversation

    def probe_capabilities(self, cwd=None) -> AgyCapabilities:
        return AgyCapabilities(
            command=["agy"],
            available=True,
            resolved="agy",
            print_flag="-p",
            supports_continue=self.supports_continue,
            supports_conversation=self.supports_conversation,
        )

    def run(
        self,
        prompt: str,
        *,
        session_id: str,
        hub: ObserverHub,
        cwd=None,
        phase: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        native_conversation_id: str | None = None,
        use_native_continue: bool = False,
    ) -> AgyRunResult:
        self.calls.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "cwd": cwd,
                "phase": phase,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "native_conversation_id": native_conversation_id,
                "use_native_continue": use_native_continue,
            }
        )
        hub.set_status(session_id, "running", "antigravity.started", {"model": DEFAULT_MODEL_LABEL, "streaming": False}, role="gemness", phase=phase)
        response = self.responses.pop(0)
        if callable(response):
            return response(prompt=prompt, session_id=session_id, hub=hub, cwd=cwd, phase=phase)
        if isinstance(response, AgyRunResult):
            if response.stdout:
                hub.append_event(session_id, "antigravity.response", "gemness", {"response": response.stdout, "streaming": False}, phase=phase)
            hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": response.exit_code, "streaming": False}, phase=phase)
            return response
        stdout = json.dumps({"response": response, "metadata": {"streaming": False, "run_id": session_id}})
        hub.append_event(session_id, "antigravity.response", "gemness", {"response": stdout, "streaming": False}, phase=phase)
        hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": 0, "streaming": False}, phase=phase)
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})


def make_service(
    tmp_path,
    responses,
    *,
    supports_continue: bool = True,
    supports_conversation: bool = True,
    **config_overrides,
) -> GemnessService:
    config = GemnessConfig(
        transcript_dir=tmp_path,
        observer_enabled=True,
        observer_port=0,
        agy_timeout_sec=2,
        **config_overrides,
    )
    return GemnessService(
        config,
        runner=FakeRunner(
            responses,
            supports_continue=supports_continue,
            supports_conversation=supports_conversation,
        ),
    )


def test_ask_antigravity_happy_path(tmp_path) -> None:
    service = make_service(tmp_path, ["hello"])
    try:
        result = service.ask_antigravity("Say hello")
        assert result["status"] == "completed"
        assert result["text"] == "hello"
        assert result["session_id"]
        assert result["metadata"]["streaming"] is False
        assert result["observer_url"].startswith("http://127.0.0.1:")
        assert service.hub.get_session(result["session_id"])["title"] == "Say hello"
        events = service.hub.get_events(result["session_id"], raw=False)
        assert "prompt.sent" in [event["type"] for event in events]
    finally:
        service.shutdown()


def test_ask_antigravity_title_uses_user_request_inside_codex_marker(tmp_path) -> None:
    prompt = """Observer에서 사용자가 지켜보는 공개 데모입니다.

Codex:
"Antigravity, Observer UX에서 Live와 History를 어떻게 나누면 좋을까?"

Antigravity의 답변을 한국어로 해주세요."""
    service = make_service(tmp_path, ["hello"])
    try:
        result = service.ask_antigravity(prompt)
        assert service.hub.get_session(result["session_id"])["title"] == "Antigravity, Observer UX에서 Live와 History를 어떻게..."
    finally:
        service.shutdown()


def test_ask_antigravity_json_valid_json(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer":"yes"}'])
    try:
        result = service.ask_antigravity_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "valid"
        assert result["data"] == {"answer": "yes"}
        assert result["repaired"] is False
        assert result["repair_attempted"] is False
        assert result["repair_succeeded"] is False
    finally:
        service.shutdown()


def test_ask_antigravity_json_invalid_then_repair_success(tmp_path) -> None:
    service = make_service(tmp_path, ["not json", '{"answer":"fixed"}'])
    try:
        result = service.ask_antigravity_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "valid"
        assert result["repaired"] is True
        assert result["repair_attempted"] is True
        assert result["repair_succeeded"] is True
        assert result["data"] == {"answer": "fixed"}
        assert len(service.runner.calls) == 2
        assert service.runner.calls[1]["phase"] == "repair"
        repair_prompt = service.runner.calls[1]["prompt"]
        assert "Previous Antigravity response:" in repair_prompt
        assert "not json" in repair_prompt
        assert "Original prompt:" not in repair_prompt
        assert "Return answer" not in repair_prompt
    finally:
        service.shutdown()


def test_ask_antigravity_json_invalid_then_repair_fail_returns_invalid(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer": 7}', '{"answer": 8}'])
    try:
        result = service.ask_antigravity_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "invalid"
        assert result["repair_attempted"] is True
        assert result["repair_succeeded"] is False
        assert "validation_errors" in result
    finally:
        service.shutdown()


def test_ask_antigravity_json_invalid_schema_returns_error_before_model_call(tmp_path) -> None:
    service = make_service(tmp_path, ['{"answer":"yes"}'])
    try:
        result = service.ask_antigravity_json("Return answer", {"type": 7})
        assert result["status"] == "error"
        assert result["message"].startswith("Invalid JSON Schema:")
        assert service.runner.calls == []
    finally:
        service.shutdown()


def test_cli_envelope_stats_and_metadata_are_returned(tmp_path) -> None:
    stdout = json.dumps({"response": "hello", "stats": {"tokens": {"total": 12}}, "metadata": {"streaming": False, "auth_status": "ok"}})
    service = make_service(tmp_path, [AgyRunResult.completed(stdout, stats={"runner": "ok"}, metadata={"streaming": False, "auth_status": "ok"})])
    try:
        result = service.ask_antigravity("Say hello")
        assert result["status"] == "completed"
        assert result["stats"]["runner"] == "ok"
        assert result["stats"]["tokens"]["total"] == 12
        assert result["metadata"]["auth_status"] == "ok"
    finally:
        service.shutdown()


def test_cli_envelope_error_returns_status_error(tmp_path) -> None:
    stdout = json.dumps({"response": "partial", "error": {"message": "auth failed"}, "metadata": {"streaming": False}})
    service = make_service(tmp_path, [AgyRunResult.completed(stdout)])
    try:
        result = service.ask_antigravity("Say hello")
        assert result["status"] == "error"
        assert "auth failed" in result["message"]
    finally:
        service.shutdown()


def test_subprocess_error_returns_status_error(tmp_path) -> None:
    service = make_service(tmp_path, [AgyRunResult.error("boom", exit_code=2, stderr="bad", metadata={"auth_status": "unknown", "streaming": False})])
    try:
        result = service.ask_antigravity_json("Return answer", TEXT_SCHEMA)
        assert result["status"] == "error"
        assert result["exit_code"] == 2
        assert result["stderr_tail"] == "bad"
        assert result["metadata"]["auth_status"] == "unknown"
    finally:
        service.shutdown()


def test_default_workspace_root_is_used_as_runner_cwd(tmp_path) -> None:
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_antigravity("Say hello")
        assert result["status"] == "completed"
        assert service.runner.calls[0]["cwd"] == tmp_path.resolve()
    finally:
        service.shutdown()


def test_requested_cwd_under_allowed_root_is_accepted(tmp_path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_antigravity("Say hello", cwd=str(child))
        assert result["status"] == "completed"
        assert service.runner.calls[0]["cwd"] == child.resolve()
    finally:
        service.shutdown()


def test_requested_cwd_outside_allowed_root_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent
    service = make_service(tmp_path / "transcripts", ["hello"], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.ask_antigravity("Say hello", cwd=str(outside))
        assert result["status"] == "error"
        assert "outside allowed roots" in result["message"]
        assert service.runner.calls == []
    finally:
        service.shutdown()


def test_review_current_diff_asks_antigravity_to_inspect_workspace(tmp_path) -> None:
    review = {
        "verdict": "pass",
        "summary": "No findings.",
        "findings": [],
        "recommended_actions": [],
    }
    service = make_service(tmp_path / "transcripts", [json.dumps(review)], workspace_root=tmp_path, allowed_roots=(tmp_path,))
    try:
        result = service.review_current_diff_with_antigravity("HEAD")
        assert result["status"] == "valid"
        assert service.runner.calls[0]["cwd"] == tmp_path.resolve()
        prompt = service.runner.calls[0]["prompt"]
        assert "Gemness has not embedded a diff" in prompt
        assert "Base ref: HEAD" in prompt
        assert FENCED_DIFF_MARKER not in prompt
        assert PATCH_HEADER_MARKER not in prompt
    finally:
        service.shutdown()


def test_review_current_diff_does_not_run_gemness_side_comparison(tmp_path, monkeypatch) -> None:
    review = {
        "verdict": "pass",
        "summary": "No findings.",
        "findings": [],
        "recommended_actions": [],
    }

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("review_current_diff_with_antigravity must not run a Gemness-side repository comparison")

    monkeypatch.setattr("gemness.tools.subprocess.run", fail_if_called)
    service = make_service(tmp_path, [json.dumps(review)])
    try:
        result = service.review_current_diff_with_antigravity("HEAD")
        assert result["status"] == "valid"
        assert result["data"]["verdict"] == "pass"
        events = service.hub.get_events(result["session_id"], raw=True)
        rendered_prompts = [event["payload"].get("prompt", "") for event in events if event["type"] == "prompt.rendered"]
        assert rendered_prompts
        assert all(FENCED_DIFF_MARKER not in prompt and PATCH_HEADER_MARKER not in prompt for prompt in rendered_prompts)
    finally:
        service.shutdown()


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


def test_prompt_sends_without_observer_approval_pause(tmp_path) -> None:
    service = make_service(tmp_path, ["sent ok"])
    try:
        result = service.ask_antigravity("original")
        events = service.hub.get_events(result["session_id"], raw=True)

        assert result["status"] == "completed"
        assert service.runner.calls[0]["prompt"] == "original"
        assert "prompt.pending_approval" not in [event["type"] for event in events]
    finally:
        service.shutdown()


def test_completed_follow_up_uses_native_conversation_id_when_available(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second"])
    try:
        first = service.ask_antigravity("first prompt")
        native_id = "9b68b5b2-b7f5-4a82-9aa3-8fcd2e831517"
        service.hub.set_agy_conversation_id(first["session_id"], native_id, source="test")
        second = service.follow_up_antigravity(first["session_id"], "go deeper")
        assert second["status"] == "completed"
        assert service.hub.get_session(second["session_id"])["parent_session_id"] == first["session_id"]
        assert service.runner.calls[1]["prompt"] == "go deeper"
        assert service.runner.calls[1]["native_conversation_id"] == native_id
        assert service.runner.calls[1]["use_native_continue"] is False
        assert second["conversation_id"] == first["conversation_id"]
    finally:
        service.shutdown()


def test_completed_follow_up_uses_native_continue_when_id_is_unavailable(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second"])
    try:
        first = service.ask_antigravity("first prompt")
        second = service.follow_up_antigravity(first["session_id"], "go deeper")
        assert second["status"] == "completed"
        assert service.runner.calls[1]["prompt"] == "go deeper"
        assert service.runner.calls[1]["native_conversation_id"] is None
        assert service.runner.calls[1]["use_native_continue"] is True
    finally:
        service.shutdown()


def test_completed_follow_up_uses_summary_when_native_flags_are_unavailable(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second"], supports_continue=False, supports_conversation=False)
    try:
        first = service.ask_antigravity("first prompt")
        service.hub.conversations[first["conversation_id"]].summary = "First answer happened."

        service.follow_up_antigravity(first["session_id"], "go deeper")

        call = service.runner.calls[1]
        assert "Context summary:\nFirst answer happened." in call["prompt"]
        assert "User follow-up:\ngo deeper" in call["prompt"]
        assert call["native_conversation_id"] is None
        assert call["use_native_continue"] is False
        assert call["fallback_used"] is True
        assert call["fallback_reason"] == "native_conversation_flags_unavailable"
    finally:
        service.shutdown()


def test_middle_turn_follow_up_creates_branch_conversation(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second", "branch"])
    try:
        first = service.ask_antigravity("first prompt")
        second = service.follow_up_antigravity(first["session_id"], "latest follow up")
        branch = service.follow_up_antigravity(first["session_id"], "branch from first")

        branch_session = service.hub.get_session(branch["session_id"])

        assert second["conversation_id"] == first["conversation_id"]
        assert branch["conversation_id"] != first["conversation_id"]
        assert branch_session["branch_from_run_id"] == first["session_id"]
        assert branch_session["fallback_used"] is True
        assert branch_session["fallback_reason"] == "branch_from_past_run"
        assert "User follow-up:" in service.runner.calls[2]["prompt"]
        assert "Recent turns:" not in service.runner.calls[2]["prompt"]
        assert service.runner.calls[2]["native_conversation_id"] is None
        assert service.runner.calls[2]["use_native_continue"] is False
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
