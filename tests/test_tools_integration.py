from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

import pytest

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
        cancel_event: threading.Event | None = None,
        process_callback=None,
        heartbeat_callback=None,
        heartbeat_interval_sec: float | None = None,
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
                "cancel_event": cancel_event,
                "heartbeat_callback": heartbeat_callback,
                "heartbeat_interval_sec": heartbeat_interval_sec,
            }
        )
        hub.set_status(session_id, "running", "antigravity.started", {"model": DEFAULT_MODEL_LABEL, "streaming": False}, role="gemness", phase=phase)
        response = self.responses.pop(0)
        if callable(response):
            return response(
                prompt=prompt,
                session_id=session_id,
                hub=hub,
                cwd=cwd,
                phase=phase,
                cancel_event=cancel_event,
                heartbeat_callback=heartbeat_callback,
            )
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
    if "workspace_root" not in config_overrides and "allowed_roots" not in config_overrides:
        config_overrides["workspace_root"] = tmp_path
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
        assert result["summary"] == "hello"
        assert result["budget"]["prompt_chars"] == len("Say hello")
        assert result["budget"]["response_chars"] == len("hello")
        assert result["budget"]["response_est_tokens"] >= 1
        assert result["budget"]["response_mode"] == "full"
        assert result["budget"]["truncated"] is False
        assert result["request_fingerprint"].startswith("req:")
        assert result["workspace_fingerprint"]
        assert result["workspace_fingerprint_degraded"] is True
        assert result["session_id"]
        assert result["metadata"]["streaming"] is False
        assert result["observer_url"].startswith("http://127.0.0.1:")
        assert service.hub.get_session(result["session_id"])["title"] == "Say hello"
        events = service.hub.get_events(result["session_id"], raw=False)
        assert "prompt.sent" in [event["type"] for event in events]
        completed = next(event for event in events if event["type"] == "session.completed")
        assert completed["payload"]["result"]["budget"]["prompt_chars"] == len("Say hello")
    finally:
        service.shutdown()


def test_ask_antigravity_filters_progress_noise_from_final_result(tmp_path) -> None:
    noisy = "\n".join(
        [
            "Searching repository files...",
            "백그라운드 작업 완료까지 대기하겠습니다.",
            "최종 검토 결과입니다.",
        ]
    )
    service = make_service(tmp_path, [noisy])
    try:
        result = service.ask_antigravity("Review")

        assert result["status"] == "completed"
        assert result["text"] == "최종 검토 결과입니다."
        assert result["filtered_progress"] is True
    finally:
        service.shutdown()


def test_ask_antigravity_preserves_advice_that_starts_like_progress(tmp_path) -> None:
    advice = "\n".join(
        [
            "Searching broadly before narrowing the scope is risky advice here.",
            "Running tests before committing is required.",
        ]
    )
    service = make_service(tmp_path, [advice])
    try:
        result = service.ask_antigravity("Review")

        assert result["status"] == "completed"
        assert result["text"] == advice
        assert "filtered_progress" not in result
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
        assert result["budget"]["prompt_chars"] > len("Return answer")
        assert result["budget"]["response_chars"] == len('{"answer":"yes"}')
        assert result["budget"]["response_est_tokens"] >= 1
        assert "raw_response" not in result
        assert result["response_preview"] == '{"answer":"yes"}'
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


def test_ask_antigravity_json_cancel_during_repair_returns_cancelled(tmp_path) -> None:
    service = make_service(tmp_path, ["not json", AgyRunResult.cancelled(metadata={"streaming": False, "cancelled": True})])
    try:
        result = service.ask_antigravity_json("Return answer", TEXT_SCHEMA)
        events = service.hub.get_events(result["session_id"], raw=True)

        assert result["status"] == "cancelled"
        assert result["repair_attempted"] is True
        assert result["repair_succeeded"] is False
        assert service.hub.get_session(result["session_id"])["status"] == "cancelled"
        assert any(event["type"] == "session.cancelled" and event.get("phase") == "repair" for event in events)
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


def test_cli_envelope_token_stats_are_preferred_for_budget(tmp_path) -> None:
    stdout = json.dumps(
        {
            "response": "hello",
            "stats": {"tokens": {"prompt_tokens": 33, "response_tokens": 7, "result_tokens": 6}},
            "metadata": {"streaming": False, "auth_status": "ok", "duration_ms": 123},
        }
    )
    service = make_service(tmp_path, [AgyRunResult.completed(stdout, metadata={"streaming": False, "auth_status": "ok"})])
    try:
        result = service.ask_antigravity("Say hello")

        assert result["budget"]["prompt_est_tokens"] == 33
        assert result["budget"]["response_est_tokens"] == 7
        assert result["budget"]["result_est_tokens"] == 6
        assert result["budget"]["duration_ms"] == 123
        assert result["budget"]["estimate_method"] == "cli_stats"
    finally:
        service.shutdown()


def test_model_json_usage_is_not_treated_as_cli_budget_stats(tmp_path) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "usage"],
        "properties": {
            "answer": {"type": "string"},
            "usage": {
                "type": "object",
                "additionalProperties": False,
                "required": ["input_tokens", "output_tokens"],
                "properties": {
                    "input_tokens": {"type": "integer"},
                    "output_tokens": {"type": "integer"},
                },
            },
        },
    }
    response = '{"answer":"yes","usage":{"input_tokens":999,"output_tokens":888}}'
    service = make_service(tmp_path, [response])
    try:
        result = service.ask_antigravity_json("Return usage", schema)

        assert result["status"] == "valid"
        assert result["budget"]["estimate_method"] == "chars_div_4"
        assert result["budget"]["prompt_est_tokens"] != 999
        assert result["budget"]["response_est_tokens"] != 888
        assert result["budget"]["response_est_tokens"] == (len(response) + 3) // 4
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
        assert result["budget"]["prompt_chars"] > 0
        assert result["budget"]["response_chars"] == len(json.dumps(review))
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
        sent = next(event for event in events if event["type"] == "prompt.sent")
        rendered = next(event for event in events if event["type"] == "prompt.rendered")
        assert rendered["payload"]["prompt"] == "original"
        assert sent["payload"]["prompt_ref"] == "prompt.rendered"
        assert sent["payload"]["prompt_preview"] == "original"
        assert "prompt" not in sent["payload"]
    finally:
        service.shutdown()


def test_start_antigravity_returns_detached_run_and_await_collects_result(tmp_path) -> None:
    release = threading.Event()

    def slow_response(prompt, session_id, hub, **kwargs):  # noqa: ANN001, ANN003
        assert prompt == "slow prompt"
        release.wait(timeout=2)
        stdout = json.dumps({"response": "slow answer", "metadata": {"streaming": False, "run_id": session_id}})
        hub.append_event(session_id, "antigravity.response", "gemness", {"response": stdout, "streaming": False})
        hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": 0, "streaming": False})
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})

    service = make_service(tmp_path, [slow_response])
    try:
        started = service.start_antigravity("slow prompt")
        assert started["status"] == "accepted"
        assert started["run_id"]
        early = service.await_antigravity_run(started["run_id"], timeout_sec=0.01)
        assert early["status"] in {"queued", "sending", "running"}
        assert "result" not in early

        release.set()
        done = service.await_antigravity_run(started["run_id"], timeout_sec=2)

        assert done["status"] == "completed"
        assert done["budget"] == done["result"]["budget"]
        assert done["result"]["text"] == "slow answer"
        assert done["result"]["summary"] == "slow answer"
        assert done["result"]["observer_url"] == done["observer_url"]
        assert done["result"]["session_id"] == started["run_id"]
        assert done["result"]["run_id"] == started["run_id"]
    finally:
        service.shutdown()


def test_idempotency_key_reuses_existing_detached_run(tmp_path) -> None:
    service = make_service(tmp_path, ["once"])
    try:
        first = service.start_antigravity("first", idempotency_key="same-request")
        second = service.start_antigravity("second", idempotency_key="same-request")
        done = service.await_antigravity_run(first["run_id"], timeout_sec=2)

        assert second["run_id"] == first["run_id"]
        assert second["idempotent"] is True
        assert done["result"]["text"] == "once"
        assert len(service.runner.calls) == 1
    finally:
        service.shutdown()


def test_request_fingerprint_is_stable_for_same_input(tmp_path) -> None:
    service = make_service(tmp_path, ["one", "two"])
    try:
        first = service.ask_antigravity("same prompt")
        second = service.ask_antigravity("same prompt")

        assert first["request_fingerprint"] == second["request_fingerprint"]
        assert first["workspace_fingerprint"] == second["workspace_fingerprint"]
    finally:
        service.shutdown()


def test_workspace_fingerprint_changes_when_workspace_changes(tmp_path) -> None:
    _require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "gemness@example.invalid")
    _git(repo, "config", "user.name", "Gemness Test")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    service = make_service(tmp_path / "transcripts", ["clean", "dirty"], workspace_root=repo, allowed_roots=(repo,))
    try:
        clean = service.ask_antigravity("fingerprint", cwd=str(repo))
        (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        dirty = service.ask_antigravity("fingerprint", cwd=str(repo))

        assert clean["workspace_fingerprint_degraded"] is False
        assert dirty["workspace_fingerprint_degraded"] is False
        assert clean["workspace_fingerprint"] != dirty["workspace_fingerprint"]
        assert clean["request_fingerprint"] != dirty["request_fingerprint"]
    finally:
        service.shutdown()


def test_raw_git_diff_is_not_exposed_in_result_or_observer_payloads(tmp_path) -> None:
    _require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "RAW_DIFF_MARKER_SHOULD_NOT_LEAK"
    _git(repo, "init")
    _git(repo, "config", "user.email", "gemness@example.invalid")
    _git(repo, "config", "user.name", "Gemness Test")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    (repo / "tracked.txt").write_text(f"after {marker}\n", encoding="utf-8")

    service = make_service(tmp_path / "transcripts", ["ok"], workspace_root=repo, allowed_roots=(repo,))
    try:
        result = service.ask_antigravity("fingerprint only", cwd=str(repo))
        raw_events = service.hub.get_events(result["session_id"], raw=True)
        public_events = service.hub.get_events(result["session_id"], raw=False)
        public_payload = json.dumps({"result": result, "raw_events": raw_events, "public_events": public_events}, ensure_ascii=False)

        assert marker not in public_payload
        assert "workspace_fingerprint" in result
    finally:
        service.shutdown()


def test_idempotency_key_concurrent_start_creates_single_detached_run(tmp_path) -> None:
    release = threading.Event()

    def slow_response(session_id, **kwargs):  # noqa: ANN001, ANN003
        release.wait(timeout=2)
        stdout = json.dumps({"response": "once", "metadata": {"streaming": False, "run_id": session_id}})
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})

    service = make_service(tmp_path, [slow_response])
    original_find = service.run_manager.find_by_idempotency_key

    def slow_find(idempotency_key):  # noqa: ANN001
        found = original_find(idempotency_key)
        if found is None:
            time.sleep(0.05)
        return found

    service.run_manager.find_by_idempotency_key = slow_find  # type: ignore[method-assign]
    try:
        start_gate = threading.Barrier(2)
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def start() -> None:
            try:
                start_gate.wait(timeout=2)
                results.append(service.start_antigravity("first", idempotency_key="same-request"))
            except BaseException as exc:  # noqa: BLE001 - preserve thread failure for assertion.
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        release.set()
        done = service.await_antigravity_run(results[0]["run_id"], timeout_sec=2)

        assert errors == []
        assert len(results) == 2
        assert {result["run_id"] for result in results} == {results[0]["run_id"]}
        assert any(result.get("idempotent") is True for result in results)
        assert done["result"]["text"] == "once"
        assert len(service.runner.calls) == 1
    finally:
        service.shutdown()


def test_follow_up_idempotency_lock_does_not_block_unrelated_start(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "independent", "follow"])
    conversation_lock = None
    try:
        first = service.ask_antigravity("first prompt")
        conversation_lock = service._conversation_lock(first["conversation_id"])
        conversation_lock.acquire()
        follow_started = threading.Event()
        follow_result: list[dict[str, Any]] = []
        follow_errors: list[BaseException] = []

        def start_follow_up() -> None:
            try:
                follow_started.set()
                follow_result.append(service.start_follow_up_antigravity(first["session_id"], "blocked follow-up", idempotency_key="follow-key"))
            except BaseException as exc:  # noqa: BLE001 - preserve thread failure for assertion.
                follow_errors.append(exc)

        follow_thread = threading.Thread(target=start_follow_up)
        follow_thread.start()
        assert follow_started.wait(timeout=1)
        time.sleep(0.05)

        started_at = time.monotonic()
        independent = service.start_antigravity("independent prompt", idempotency_key="independent-key")
        elapsed = time.monotonic() - started_at

        conversation_lock.release()
        follow_thread.join(timeout=2)
        independent_done = service.await_antigravity_run(independent["run_id"], timeout_sec=2)
        follow_done = service.await_antigravity_run(follow_result[0]["run_id"], timeout_sec=2)

        assert elapsed < 0.5
        assert follow_errors == []
        assert independent_done["status"] == "completed"
        assert follow_done["status"] == "completed"
    finally:
        if conversation_lock is not None and conversation_lock.locked():
            conversation_lock.release()
        service.shutdown()


def test_get_antigravity_run_event_cursor_returns_only_later_events(tmp_path) -> None:
    release = threading.Event()

    def slow_response(session_id, **kwargs):  # noqa: ANN001, ANN003
        release.wait(timeout=2)
        stdout = json.dumps({"response": "cursor ok", "metadata": {"streaming": False, "run_id": session_id}})
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})

    service = make_service(tmp_path, [slow_response])
    try:
        started = service.start_antigravity("cursor")
        first = service.get_antigravity_run(started["run_id"])
        cursor = first["next_event_cursor"]
        release.set()
        done = service.await_antigravity_run(started["run_id"], timeout_sec=2, event_cursor=cursor)

        assert done["status"] == "completed"
        assert done["events"]
        assert all(event["event_id"] != cursor for event in done["events"])
    finally:
        service.shutdown()


def test_completed_detached_run_is_evictable_and_recovers_from_events(tmp_path) -> None:
    service = make_service(tmp_path, ["once"])
    try:
        started = service.start_antigravity("first", idempotency_key="same-request")
        done = service.await_antigravity_run(started["run_id"], timeout_sec=2)
        repeated = service.start_antigravity("first", idempotency_key="same-request")

        assert done["status"] == "completed"
        assert service.run_manager.get(started["run_id"]) is None
        assert repeated["run_id"] == started["run_id"]
        assert repeated["idempotent"] is True
        assert repeated["result"]["text"] == "once"
        assert len(service.runner.calls) == 1
    finally:
        service.shutdown()


def test_start_antigravity_rejects_when_run_queue_is_full(tmp_path) -> None:
    release = threading.Event()

    def slow_response(session_id, **kwargs):  # noqa: ANN001, ANN003
        release.wait(timeout=2)
        stdout = json.dumps({"response": "done", "metadata": {"streaming": False, "run_id": session_id}})
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})

    service = make_service(tmp_path, [slow_response, slow_response], agy_concurrency_limit=1, agy_queue_limit=1)
    try:
        first = service.start_antigravity("first")
        _wait_for_session_status(service.hub, "running")
        second = service.start_antigravity("second")
        third = service.start_antigravity("third")
        release.set()
        first_done = service.await_antigravity_run(first["run_id"], timeout_sec=2)
        second_done = service.await_antigravity_run(second["run_id"], timeout_sec=2)

        assert first_done["status"] == "completed"
        assert second_done["status"] == "completed"
        assert third["status"] == "error"
        assert third["result"]["reason"] == "run_queue_full"
        assert len(service.runner.calls) == 2
    finally:
        service.shutdown()


def test_cancel_antigravity_run_marks_run_cancelled(tmp_path) -> None:
    observed_cancel = threading.Event()

    def cancellable_response(session_id, cancel_event, **kwargs):  # noqa: ANN001, ANN003
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                observed_cancel.set()
                return AgyRunResult.cancelled(metadata={"streaming": False, "cancelled": True})
            time.sleep(0.01)
        return AgyRunResult.completed(json.dumps({"response": "too late", "metadata": {"streaming": False, "run_id": session_id}}))

    service = make_service(tmp_path, [cancellable_response])
    try:
        started = service.start_antigravity("cancel me")
        _wait_for_session_status(service.hub, "running")

        cancelled = service.cancel_antigravity_run(started["run_id"])
        done = service.await_antigravity_run(started["run_id"], timeout_sec=2)

        assert cancelled["cancel"]["status"] in {"cancelling", "cancelled"}
        assert done["status"] == "cancelled"
        assert observed_cancel.is_set()
    finally:
        service.shutdown()


def test_heartbeat_callback_records_observer_event(tmp_path) -> None:
    def heartbeat_response(session_id, heartbeat_callback, **kwargs):  # noqa: ANN001, ANN003
        heartbeat_callback({"elapsed_ms": 10, "timeout_remaining_ms": 1990, "pid": 123, "capture_mode": "test", "stdout_bytes": 0, "stderr_bytes": 0})
        stdout = json.dumps({"response": "heartbeat ok", "metadata": {"streaming": False, "run_id": session_id}})
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})

    service = make_service(tmp_path, [heartbeat_response])
    try:
        started = service.start_antigravity("heartbeat")
        done = service.await_antigravity_run(started["run_id"], timeout_sec=2)
        events = service.hub.get_events(started["run_id"], raw=True)

        assert done["status"] == "completed"
        heartbeat = next(event for event in events if event["type"] == "antigravity.heartbeat")
        assert heartbeat["payload"]["pid"] == 123
        assert heartbeat["payload"]["timeout_remaining_ms"] == 1990
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
        assert "first" not in service.runner.calls[1]["prompt"]
        assert "first prompt" not in service.runner.calls[1]["prompt"]
        assert service.runner.calls[1]["native_conversation_id"] == native_id
        assert second["conversation_id"] == first["conversation_id"]
    finally:
        service.shutdown()


def test_completed_follow_up_uses_summary_when_native_id_is_unavailable(tmp_path) -> None:
    service = make_service(tmp_path, ["first", "second"])
    try:
        first = service.ask_antigravity("first prompt")
        service.hub.conversations[first["conversation_id"]].summary = "First answer happened."
        second = service.follow_up_antigravity(first["session_id"], "go deeper")
        assert second["status"] == "completed"
        call = service.runner.calls[1]
        assert "Context summary:\nFirst answer happened." in call["prompt"]
        assert "User follow-up:\ngo deeper" in call["prompt"]
        assert call["native_conversation_id"] is None
        assert call["fallback_used"] is True
        assert call["fallback_reason"] == "native_conversation_id_unavailable"
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
        assert call["fallback_used"] is True
        assert call["fallback_reason"] == "native_conversation_flag_unavailable"
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


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for workspace fingerprint tests")


def _git(cwd, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout
