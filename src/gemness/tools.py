from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_host import codex_host_capabilities
from .config import DEFAULT_MODEL_LABEL, GemnessConfig
from .json_utils import extract_cli_response, parse_json_candidate
from .mcp_metadata import SERVER_NAME, SERVER_VERSION, TOOL_NAMES
from .observer import FINAL_STATUSES, ObserverHub
from .review import (
    REVIEW_SCHEMA,
    ReviewWorkspace,
    ReviewWorkspaceError,
    build_review_prompt,
    inspect_review_workspace,
    validate_review_scope,
)
from .run_manager import RunManager
from .runner import AgyCliRunner, AgyRunResult, AgyRunner, agy_fallback_paths, command_exists, probe_auth, resolve_agy_command
from .schema_validation import validate_json_schema, validate_schema_definition
from .telemetry import RequestProvenance, build_budget, build_request_provenance, combine_budgets
from .workspace import WorkspaceAccessError, inspect_workspace_policy, normalized_allowed_roots, resolve_workspace_cwd


RESPONSE_PREVIEW_CHARS = 4000
PROGRESS_NOISE_PATTERNS = (
    re.compile(r"^\s*(?:Searching|Reading|Inspecting|Scanning|Running|Waiting)\b.{0,160}(?:\.\.\.|…)\s*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:I(?:'|’)ll|I will|I(?:'|’)m going to|Let me)\s+"
        r"(?:inspect|search|check|look|scan|review|find|run|open|read)\b.{0,160}(?:\.\.\.|…)\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*.*(?:background task|background job).*(?:wait|waiting|complete|finished).*$", re.IGNORECASE),
    re.compile(r"^\s*.*백그라운드\s*작업.*(?:대기|기다|완료|종료).*$"),
    re.compile(r"^\s*.*(?:파일|코드|워크스페이스|리포지토리|저장소).*(?:검색|확인|살펴보|찾아보).*(?:중|겠습니다|볼게요).*$"),
    re.compile(r"^\s*(?:잠시\s*)?(?:대기|기다리)겠(?:습니다|어요)\.?\s*$"),
)


@dataclass(slots=True)
class _FollowUpPlan:
    prompt: str
    conversation_id: str | None
    parent_session_id: str | None
    parent_run_id: str | None
    branch_from_conversation_id: str | None
    branch_from_run_id: str | None
    cwd: Path | None
    native_conversation_id: str | None
    fallback_used: bool
    fallback_reason: str | None


class GemnessService:
    def __init__(
        self,
        config: GemnessConfig | None = None,
        *,
        hub: ObserverHub | None = None,
        runner: AgyRunner | None = None,
    ) -> None:
        self.config = config or GemnessConfig.from_env()
        self.hub = hub or ObserverHub(self.config)
        self.hub.attach_service(self)
        self.runner = runner or AgyCliRunner(self.config)
        self.run_manager = RunManager(self.config, self.hub)
        self._idempotency_locks: dict[str, threading.Lock] = {}
        self._idempotency_locks_guard = threading.RLock()
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._conversation_locks_guard = threading.RLock()
        if self.config.observer_enabled and self.config.observer_start_on_init:
            self.hub.start_web_server()

    def shutdown(self) -> None:
        self.run_manager.shutdown()
        self.hub.shutdown()

    def antigravity_health(
        self,
        *,
        cwd: str | None = None,
        check_antigravity: bool = True,
        codex_multi_agent_available: bool | None = None,
        codex_multi_agent_evidence: str | None = None,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        codex_host, codex_host_warnings = codex_host_capabilities(
            self.config.codex_host_capabilities_file,
            multi_agent_available=codex_multi_agent_available,
            evidence=codex_multi_agent_evidence,
        )
        warnings.extend(codex_host_warnings)
        workspace_decision = inspect_workspace_policy(self.config, cwd)
        resolved_cwd = workspace_decision.cwd if workspace_decision.allowed else None
        cwd_error = workspace_decision.message if not workspace_decision.allowed else None
        if cwd_error:
            warnings.append(cwd_error)
        warnings.extend(workspace_decision.diagnostics)

        transcript_dir = Path(self.config.transcript_dir).expanduser().resolve()
        transcript_writable = _is_writable_dir(transcript_dir)
        if not transcript_writable:
            warnings.append(f"Transcript directory is not writable: {transcript_dir}")

        command_parts = resolve_agy_command(self.config.agy_command)
        capabilities = None
        auth_probe = {"status": "not_checked", "message": "Antigravity CLI auth probe was skipped."}
        antigravity: dict[str, Any] = {
            "command": self.config.agy_command,
            "resolved": command_parts[0] if command_parts else self.config.agy_command,
            "argv": command_parts,
            "available": command_exists(self.config.agy_command),
            "fallback_paths": [str(path) for path in agy_fallback_paths()],
            "streaming": False,
            "model_selection": "Use Antigravity CLI settings or `/model`; Gemness does not pass model flags.",
        }
        if check_antigravity and resolved_cwd is not None:
            probe_method = getattr(self.runner, "probe_capabilities", None)
            capabilities = probe_method(resolved_cwd) if callable(probe_method) else AgyCliRunner(self.config).probe_capabilities(resolved_cwd)
            antigravity["capabilities"] = capabilities.to_dict()
            antigravity["version"] = capabilities.version
            antigravity["print_mode"] = {"supported": capabilities.print_supported, "flag": capabilities.print_flag}
            antigravity["conversation_flags"] = {
                "continue": capabilities.supports_continue,
                "conversation": capabilities.supports_conversation,
                "used_by_gemness": capabilities.supports_conversation,
            }
            if capabilities.error:
                warnings.append(capabilities.error)
            warnings.extend(capabilities.warnings)
            if capabilities.available and capabilities.print_supported:
                auth_probe = probe_auth(capabilities.command, capabilities.print_flag, resolved_cwd, self.config).to_dict()
                if auth_probe["status"] in {"auth_required", "unknown"}:
                    warnings.append(auth_probe["message"])
        elif check_antigravity:
            warnings.append("Antigravity CLI active probe skipped because the workspace cwd is not allowed.")
        antigravity["auth"] = auth_probe

        workspace = workspace_decision.to_workspace_payload() | {
            "is_git_repo": _is_git_repo(workspace_decision.cwd) if workspace_decision.exists and workspace_decision.is_dir else False,
            "allowed_roots": [str(root) for root in normalized_allowed_roots(self.config)],
        }
        observer_url = self.hub.base_url if self.config.observer_enabled else ""
        observer = {
            "enabled": self.config.observer_enabled,
            "host": self.config.observer_host,
            "port": self.config.observer_port,
            "start_on_init": self.config.observer_start_on_init,
            "running": self.hub.web_server_running,
            "url": observer_url,
        }
        status = "warning" if warnings else "ok"
        return {
            "status": status,
            "server": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "python": sys.version.split()[0],
                "executable": sys.executable,
            },
            "mcp": {"transport": "stdio", "tools": TOOL_NAMES},
            "codex_host": codex_host,
            "workspace": workspace,
            "antigravity": antigravity,
            "observer": observer,
            "transcript": {"dir": str(transcript_dir), "writable": transcript_writable},
            "warnings": warnings,
        }

    def ask_antigravity(self, prompt: str, cwd: str | None = None) -> dict[str, Any]:
        started = self.start_antigravity(prompt, cwd=cwd)
        if started.get("status") == "error":
            return started
        return self._await_blocking(str(started["run_id"]))

    def ask_antigravity_json(self, prompt: str, schema: dict[str, Any], cwd: str | None = None) -> dict[str, Any]:
        started = self.start_antigravity_json(prompt, schema, cwd=cwd)
        if started.get("status") == "error":
            return started
        return self._await_blocking(str(started["run_id"]))

    def review_current_diff_with_antigravity(self, base_ref: str = "HEAD", cwd: str | None = None) -> dict[str, Any]:
        started = self.start_review_current_diff_with_antigravity(base_ref=base_ref, cwd=cwd)
        if started.get("status") == "error":
            return started
        return self._await_blocking(str(started["run_id"]))

    def follow_up_antigravity(self, parent_session_id: str, instruction: str) -> dict[str, Any]:
        started = self.start_follow_up_antigravity(parent_session_id, instruction)
        if started.get("status") == "error":
            return started
        return self._await_blocking(str(started["run_id"]))

    def start_antigravity(self, prompt: str, cwd: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except WorkspaceAccessError as exc:
            return exc.to_payload()
        provenance = self._request_provenance("ask", prompt, resolved_cwd)
        idempotency_context = _idempotency_context("ask_antigravity", resolved_cwd, provenance)
        with self._idempotency_scope(idempotency_key, idempotency_context):
            existing = self._existing_idempotent_run(idempotency_key, idempotency_context=idempotency_context)
            if existing is not None:
                return existing
            session = self._session("ask_antigravity", None, None, _session_title(prompt, "ask_antigravity"), cwd=resolved_cwd)
            self._record_request_provenance(session.session_id, provenance)
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_text_session(
                    "ask_antigravity",
                    prompt,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=prompt,
                    request_provenance=provenance,
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
                idempotency_context=idempotency_context,
            )
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def start_antigravity_json(self, prompt: str, schema: dict[str, Any], cwd: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        schema_error = validate_schema_definition(schema)
        if schema_error:
            return {"status": "error", "message": f"Invalid JSON Schema: {schema_error}"}
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except WorkspaceAccessError as exc:
            return exc.to_payload()
        provenance = self._request_provenance("json", prompt, resolved_cwd, schema=schema)
        idempotency_context = _idempotency_context("ask_antigravity_json", resolved_cwd, provenance, schema_hash=provenance.schema_hash)
        with self._idempotency_scope(idempotency_key, idempotency_context):
            existing = self._existing_idempotent_run(idempotency_key, idempotency_context=idempotency_context)
            if existing is not None:
                return existing
            session = self._session("ask_antigravity_json", None, None, _session_title(prompt, "ask_antigravity_json"), cwd=resolved_cwd)
            self._record_request_provenance(session.session_id, provenance)
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_json_session(
                    "ask_antigravity_json",
                    prompt,
                    schema,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=prompt,
                    request_provenance=provenance,
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
                idempotency_context=idempotency_context,
            )
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def start_review_current_diff_with_antigravity(self, base_ref: str = "HEAD", cwd: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except WorkspaceAccessError as exc:
            return exc.to_payload()
        try:
            base_ref = validate_base_ref(base_ref)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        try:
            review_workspace = inspect_review_workspace(resolved_cwd, base_ref)
        except ReviewWorkspaceError as exc:
            return exc.to_payload()
        prompt = build_review_prompt(base_ref, review_workspace)
        provenance = self._request_provenance("review_current_diff", prompt, resolved_cwd, base_ref=base_ref)
        idempotency_context = _idempotency_context(
            "review_current_diff_with_antigravity",
            resolved_cwd,
            provenance,
            base_ref=base_ref,
            workspace_root=str(review_workspace.workspace_root),
        )
        with self._idempotency_scope(idempotency_key, idempotency_context):
            existing = self._existing_idempotent_run(idempotency_key, idempotency_context=idempotency_context)
            if existing is not None:
                return existing
            session = self.hub.create_session(
                "review_current_diff_with_antigravity",
                DEFAULT_MODEL_LABEL,
                title=f"현재 변경 리뷰: {base_ref}",
                project_root=str(resolved_cwd),
            )
            self._record_request_provenance(session.session_id, provenance)
            self.hub.append_event(session.session_id, "review.scope", "system", review_workspace.to_payload())
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_json_session(
                    "review_current_diff_with_antigravity",
                    prompt,
                    REVIEW_SCHEMA,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=f"현재 변경 리뷰: {base_ref}",
                    request_provenance=provenance,
                    review_workspace=review_workspace,
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
                idempotency_context=idempotency_context,
            )
            payload = self._start_payload(session.session_id, idempotency_key=idempotency_key)
            payload["expected_review_scope"] = review_workspace.to_payload()
            return payload

    def start_follow_up_antigravity(self, parent_session_id: str, instruction: str, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._idempotency_scope(idempotency_key):
            existing = self._existing_idempotent_run(idempotency_key)
            if existing is not None:
                return existing
            self.hub.refresh_from_disk()
            if parent_session_id not in self.hub.sessions:
                return {"status": "error", "message": f"Unknown parent_session_id: {parent_session_id}"}
            try:
                plan = self._with_allowed_plan_cwd(self._follow_up_plan(parent_session_id, instruction))
            except WorkspaceAccessError as exc:
                return exc.to_payload()
            create_lock = self._conversation_lock(plan.conversation_id) if plan.conversation_id else _null_lock()
            with create_lock:
                if self.hub.is_latest_run(parent_session_id):
                    try:
                        plan = self._with_allowed_plan_cwd(self._follow_up_plan(parent_session_id, instruction))
                    except WorkspaceAccessError as exc:
                        return exc.to_payload()
                session = self.hub.create_session(
                    "ask_antigravity",
                    DEFAULT_MODEL_LABEL,
                    parent_session_id=plan.parent_session_id,
                    title=_session_title(instruction, "ask_antigravity"),
                    conversation_id=plan.conversation_id,
                    parent_run_id=plan.parent_run_id,
                    branch_from_conversation_id=plan.branch_from_conversation_id,
                    branch_from_run_id=plan.branch_from_run_id,
                    project_root=str(plan.cwd) if plan.cwd is not None else None,
                    agy_conversation_id=plan.native_conversation_id,
                    fallback_used=plan.fallback_used,
                    fallback_reason=plan.fallback_reason,
                )
                provenance = self._request_provenance(
                    "follow_up",
                    plan.prompt,
                    plan.cwd,
                    parent_session_id=parent_session_id,
                    native_conversation_id=plan.native_conversation_id,
                )
                self._record_request_provenance(session.session_id, provenance)
            run_lock = self._conversation_lock(session.conversation_id)

            def run(cancel_event, process_callback, heartbeat_callback) -> dict[str, Any]:
                with run_lock:
                    return self._run_text_session(
                        "ask_antigravity",
                        plan.prompt,
                        parent_session_id=plan.parent_session_id,
                        existing_session_id=session.session_id,
                        cwd=plan.cwd,
                        title_source=instruction,
                        fallback_used=plan.fallback_used,
                        fallback_reason=plan.fallback_reason,
                        native_conversation_id=plan.native_conversation_id,
                        request_provenance=provenance,
                        cancel_event=cancel_event,
                        process_callback=process_callback,
                        heartbeat_callback=heartbeat_callback,
                    )

            self.run_manager.start(session.session_id, run, idempotency_key=idempotency_key)
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def get_antigravity_run(self, run_id: str, event_cursor: str | None = None, recent_event_limit: int = 20) -> dict[str, Any]:
        return self._run_status_payload(run_id, event_cursor=event_cursor, recent_event_limit=recent_event_limit)

    def await_antigravity_run(self, run_id: str, timeout_sec: float = 5.0, event_cursor: str | None = None, recent_event_limit: int = 20) -> dict[str, Any]:
        self.run_manager.await_run(run_id, timeout_sec)
        return self._run_status_payload(run_id, event_cursor=event_cursor, recent_event_limit=recent_event_limit)

    def cancel_antigravity_run(self, run_id: str) -> dict[str, Any]:
        cancelled = self.run_manager.cancel(run_id)
        return self._run_status_payload(run_id) | {"cancel": cancelled}

    def start_follow_up(self, parent_session_id: str, instruction: str) -> str:
        return str(self.start_follow_up_antigravity(parent_session_id, instruction)["run_id"])

    def _await_blocking(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + max(10.0, (self.config.agy_timeout_sec * 2) + 60.0)
        while True:
            status = self.await_antigravity_run(run_id, timeout_sec=5, recent_event_limit=0)
            if status.get("status") in FINAL_STATUSES:
                result = status.get("result")
                return result if isinstance(result, dict) else status
            if time.monotonic() >= deadline:
                return status | {"message": "Blocking compatibility wait reached its safety deadline."}

    def _existing_idempotent_run(self, idempotency_key: str | None, *, idempotency_context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        existing_run_id = self.run_manager.find_by_idempotency_key(idempotency_key, idempotency_context=idempotency_context)
        if existing_run_id is None:
            return None
        return self._run_status_payload(existing_run_id) | {"idempotent": True}

    def _idempotency_scope(self, idempotency_key: str | None, idempotency_context: dict[str, Any] | None = None) -> Any:
        key = _clean_idempotency_key(idempotency_key)
        if key is None:
            return _null_lock()
        key = _idempotency_lock_key(key, idempotency_context)
        with self._idempotency_locks_guard:
            lock = self._idempotency_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._idempotency_locks[key] = lock
            return lock

    def _start_payload(self, run_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        session = self.hub.get_session(run_id, raw=True)
        status = str(session["status"])
        raw_events = self.hub.get_events(run_id, raw=True)
        result = _event_result(raw_events) if status in FINAL_STATUSES else None
        rejected = status == "error" and isinstance(result, dict) and result.get("reason") == "run_queue_full"
        payload = {
            "status": "error" if rejected else "accepted",
            "run_id": session["run_id"],
            "session_id": session["session_id"],
            "conversation_id": session.get("conversation_id"),
            "observer_url": session["observer_url"],
            "session_status": status,
            "idempotency_key": idempotency_key,
        }
        payload.update(_event_provenance_fields(raw_events))
        if result is not None:
            payload["result"] = result
            if isinstance(result.get("budget"), dict):
                payload["budget"] = result["budget"]
            if isinstance(result.get("message"), str):
                payload["message"] = result["message"]
        return payload

    def _run_status_payload(
        self,
        run_id: str,
        *,
        event_cursor: str | None = None,
        recent_event_limit: int = 20,
    ) -> dict[str, Any]:
        try:
            session = self.hub.get_session(run_id, raw=True)
        except KeyError:
            return {"status": "error", "run_id": run_id, "message": f"Unknown run_id: {run_id}"}
        raw_events = self.hub.get_events(run_id, raw=True)
        public_events = self.hub.get_events(run_id, raw=False)
        events = _events_after_cursor(public_events, event_cursor)
        recent_event_limit = max(0, min(int(recent_event_limit), 100))
        if recent_event_limit:
            events = events[-recent_event_limit:]
        else:
            events = []
        managed = self.run_manager.get(run_id)
        result = managed.result if managed is not None and managed.result is not None else _event_result(raw_events)
        next_cursor = raw_events[-1]["event_id"] if raw_events else event_cursor
        payload = {
            "status": session["status"],
            "run_id": session["run_id"],
            "session_id": session["session_id"],
            "conversation_id": session.get("conversation_id"),
            "observer_url": session["observer_url"],
            "terminal": session["status"] in FINAL_STATUSES,
            "events": events,
            "next_event_cursor": next_cursor,
        }
        if result is not None:
            payload["result"] = result
            if isinstance(result.get("budget"), dict):
                payload["budget"] = result["budget"]
        payload.update(_event_provenance_fields(raw_events))
        return payload

    def _call_runner(self, prompt: str, **kwargs: Any) -> AgyRunResult:
        run = self.runner.run
        signature = inspect.signature(run)
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return run(prompt, **kwargs)
        accepted = {name for name, parameter in signature.parameters.items() if parameter.kind in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}}
        filtered = {key: value for key, value in kwargs.items() if key in accepted}
        return run(prompt, **filtered)

    def _run_text_session(
        self,
        tool_name: str,
        prompt: str,
        *,
        parent_session_id: str | None = None,
        existing_session_id: str | None = None,
        cwd: Path | None = None,
        title_source: str | None = None,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        native_conversation_id: str | None = None,
        request_provenance: RequestProvenance | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Any = None,
        heartbeat_callback: Any = None,
    ) -> dict[str, Any]:
        title = _session_title(title_source or prompt, tool_name)
        session = self._session(
            tool_name,
            parent_session_id,
            existing_session_id,
            title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
            cwd=cwd,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        session_id = session.session_id
        if not session.title:
            self.hub.set_title(session_id, title)
        observer_url = self.hub.observer_url(session_id)
        if request_provenance is None:
            request_provenance = self._request_provenance(
                _mode_for_tool(tool_name),
                prompt,
                cwd,
                parent_session_id=parent_session_id,
                native_conversation_id=native_conversation_id,
            )
        self._record_request_provenance(session_id, request_provenance)
        prompt_to_send = self.hub.prepare_prompt(session_id, prompt)

        runner_started = time.monotonic()
        result = self._call_runner(
            prompt_to_send,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            native_conversation_id=native_conversation_id,
            cancel_event=cancel_event,
            process_callback=process_callback,
            heartbeat_callback=heartbeat_callback,
            heartbeat_interval_sec=self.config.agy_heartbeat_interval_sec,
        )
        runner_duration_ms = _elapsed_ms(runner_started)
        if result.status == "interrupted":
            retry_result = self._retry_text(tool_name, session_id, prompt_to_send, result, cwd)
            self.hub.set_status(
                session_id,
                "cancelled",
                "session.cancelled",
                {"reason": "interrupted_and_retried", "child_session_id": retry_result.get("session_id")},
            )
            return retry_result
        if result.status == "cancelled":
            response_text, envelope = extract_cli_response(result.stdout)
            metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
            budget = build_budget(
                prompt=prompt_to_send,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=metadata,
            )
            payload = {
                "status": "cancelled",
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "metadata": metadata,
                "budget": budget,
                **request_provenance.result_fields(),
            }
            self.hub.set_status(session_id, "cancelled", "session.cancelled", payload | {"reason": "user_cancelled"})
            return payload
        if result.status == "error":
            response_text, envelope = extract_cli_response(result.stdout)
            metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
            budget = build_budget(
                prompt=prompt_to_send,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=metadata,
            )
            return self._runner_error(session_id, observer_url, result, budget=budget, request_provenance=request_provenance, metadata=metadata)

        text, envelope = extract_cli_response(result.stdout)
        clean_text = clean_advisory_text(text)
        stats = _merged_stats(result.stats, envelope)
        metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
        budget = build_budget(
            prompt=prompt_to_send,
            response=text,
            raw_stdout=result.raw_stdout or result.stdout,
            result=clean_text,
            duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
            envelope=envelope,
            stats=stats,
            metadata=metadata,
        )
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats, metadata, budget=budget, request_provenance=request_provenance)
        payload = {
            "status": "completed",
            "text": clean_text,
            "summary": _advisory_summary(clean_text),
            "session_id": session_id,
            "run_id": session.run_id or session_id,
            "conversation_id": session.conversation_id,
            "observer_url": observer_url,
            "stats": stats,
            "metadata": metadata,
            "budget": budget,
            **request_provenance.result_fields(),
        }
        if clean_text != text:
            payload["filtered_progress"] = True
        if envelope is not None:
            payload["stats"] = stats | {"cli_envelope_keys": sorted(envelope.keys())}
        self.hub.set_status(session_id, "completed", "session.completed", {"result": payload})
        return payload

    def _run_json_session(
        self,
        tool_name: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        parent_session_id: str | None = None,
        existing_session_id: str | None = None,
        warnings: list[str] | None = None,
        cwd: Path | None = None,
        title_source: str | None = None,
        request_provenance: RequestProvenance | None = None,
        review_workspace: ReviewWorkspace | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Any = None,
        heartbeat_callback: Any = None,
    ) -> dict[str, Any]:
        schema_error = validate_schema_definition(schema)
        if schema_error:
            return {"status": "error", "message": f"Invalid JSON Schema: {schema_error}"}
        title = _session_title(title_source or prompt, tool_name)
        session = self._session(tool_name, parent_session_id, existing_session_id, title, cwd=cwd)
        session_id = session.session_id
        observer_url = self.hub.observer_url(session_id)
        if request_provenance is None:
            request_provenance = self._request_provenance(_mode_for_tool(tool_name), prompt, cwd, schema=schema, parent_session_id=parent_session_id)
        self._record_request_provenance(session_id, request_provenance)
        prompt_to_send = self.hub.prepare_prompt(session_id, _json_prompt(prompt, schema))

        runner_started = time.monotonic()
        result = self._call_runner(
            prompt_to_send,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            cancel_event=cancel_event,
            process_callback=process_callback,
            heartbeat_callback=heartbeat_callback,
            heartbeat_interval_sec=self.config.agy_heartbeat_interval_sec,
        )
        runner_duration_ms = _elapsed_ms(runner_started)
        if result.status == "interrupted":
            retry_result = self._retry_json(tool_name, session_id, prompt_to_send, result, schema, cwd)
            self.hub.set_status(
                session_id,
                "cancelled",
                "session.cancelled",
                {"reason": "interrupted_and_retried", "child_session_id": retry_result.get("session_id")},
            )
            return retry_result
        if result.status == "cancelled":
            response_text, envelope = extract_cli_response(result.stdout)
            metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
            budget = build_budget(
                prompt=prompt_to_send,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=metadata,
            )
            payload = {
                "status": "cancelled",
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "metadata": metadata,
                "budget": budget,
                **request_provenance.result_fields(),
            }
            self.hub.set_status(session_id, "cancelled", "session.cancelled", payload | {"reason": "user_cancelled"})
            return payload
        if result.status == "error":
            response_text, envelope = extract_cli_response(result.stdout)
            metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
            budget = build_budget(
                prompt=prompt_to_send,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=metadata,
            )
            return self._runner_error(session_id, observer_url, result, budget=budget, request_provenance=request_provenance, metadata=metadata)

        response_text, envelope = extract_cli_response(result.stdout)
        stats = _merged_stats(result.stats, envelope)
        metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance)
        budget = build_budget(
            prompt=prompt_to_send,
            response=response_text,
            raw_stdout=result.raw_stdout or result.stdout,
            result=response_text,
            duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
            envelope=envelope,
            stats=stats,
            metadata=metadata,
        )
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats, metadata, budget=budget, request_provenance=request_provenance)
        data, parse_error, candidate = parse_json_candidate(response_text)
        self.hub.append_event(
            session_id,
            "json.extracted",
            "codex_mcp",
            {"candidate": candidate, "parse_error": parse_error, "stats": stats},
        )
        if parse_error is None:
            validation_errors = validate_json_schema(data, schema)
        else:
            validation_errors = []
        if parse_error is None and not validation_errors:
            review_scope_errors = validate_review_scope(data, review_workspace) if review_workspace is not None else []
            if review_scope_errors:
                return self._review_scope_invalid_result(
                    session_id,
                    observer_url,
                    response_text,
                    review_scope_errors,
                    stats,
                    metadata,
                    budget,
                    warnings or [],
                    request_provenance,
                    repaired=False,
                    repair_attempted=False,
                    repair_succeeded=False,
                )
            self.hub.append_event(session_id, "json.validation_passed", "codex_mcp", {"data": data})
            payload = {
                "status": "valid",
                "data": data,
                "response_preview": _preview_text(response_text),
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "stats": stats,
                "metadata": metadata,
                "budget": build_budget(
                    prompt=prompt_to_send,
                    response=response_text,
                    raw_stdout=result.raw_stdout or result.stdout,
                    result=_json_result_text(data),
                    duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                    envelope=envelope,
                    stats=stats,
                    metadata=metadata,
                ),
                "warnings": warnings or [],
                "repaired": False,
                "repair_attempted": False,
                "repair_succeeded": False,
                **request_provenance.result_fields(),
            }
            payload.update(_review_payload_fields(data, review_workspace))
            self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
            return payload
        repair = self._repair_or_invalid(
            session_id,
            schema,
            response_text,
            parse_error,
            validation_errors,
            cwd,
            request_provenance=request_provenance,
            cancel_event=cancel_event,
            process_callback=process_callback,
            heartbeat_callback=heartbeat_callback,
        )
        combined_budget = combine_budgets(budget, repair.get("budget"))
        if repair["status"] == "valid":
            review_scope_errors = validate_review_scope(repair["data"], review_workspace) if review_workspace is not None else []
            if review_scope_errors:
                return self._review_scope_invalid_result(
                    session_id,
                    observer_url,
                    repair["raw_response"],
                    review_scope_errors,
                    stats,
                    metadata,
                    combined_budget or budget,
                    warnings or [],
                    request_provenance,
                    repaired=False,
                    repair_attempted=True,
                    repair_succeeded=True,
                )
            payload = {
                "status": "valid",
                "data": repair["data"],
                "response_preview": _preview_text(repair["raw_response"]),
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "stats": stats,
                "metadata": metadata,
                "budget": combined_budget or budget,
                "warnings": warnings or [],
                "repaired": True,
                "repair_attempted": True,
                "repair_succeeded": True,
                **request_provenance.result_fields(),
            }
            payload.update(_review_payload_fields(repair["data"], review_workspace))
            self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
            return payload
        if repair["status"] == "error":
            repair_metadata = _metadata_with_provenance(repair["result"].metadata, request_provenance)
            return self._runner_error(
                session_id,
                observer_url,
                repair["result"],
                budget=combined_budget or budget,
                request_provenance=request_provenance,
                metadata=repair_metadata,
            )
        if repair["status"] == "interrupted":
            retry_result = self._retry_json(tool_name, session_id, prompt_to_send, repair["result"], schema, cwd)
            self.hub.set_status(
                session_id,
                "cancelled",
                "session.cancelled",
                {"reason": "repair_interrupted_and_retried", "child_session_id": retry_result.get("session_id")},
                phase="repair",
            )
            return retry_result
        if repair["status"] == "cancelled":
            payload = {
                "status": "cancelled",
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "metadata": _metadata_with_provenance(repair["result"].metadata, request_provenance),
                "budget": combined_budget or budget,
                "repaired": False,
                "repair_attempted": True,
                "repair_succeeded": False,
                **request_provenance.result_fields(),
            }
            self.hub.set_status(session_id, "cancelled", "session.cancelled", payload | {"reason": "user_cancelled", "phase": "repair"}, phase="repair")
            return payload
        payload = {
            "status": "invalid",
            "response_preview": _preview_text(response_text),
            "parse_error": parse_error,
            "validation_errors": validation_errors,
            "session_id": session_id,
            "run_id": session.run_id or session_id,
            "conversation_id": session.conversation_id,
            "observer_url": observer_url,
            "stats": stats,
            "metadata": metadata,
            "budget": combined_budget or budget,
            "warnings": warnings or [],
            "repaired": False,
            "repair_attempted": True,
            "repair_succeeded": False,
            **request_provenance.result_fields(),
        }
        event_name = "json.parse_failed" if parse_error else "json.validation_failed"
        self.hub.append_event(session_id, event_name, "codex_mcp", {"parse_error": parse_error, "validation_errors": validation_errors})
        self.hub.set_status(session_id, "invalid", "session.completed", {"result": payload})
        return payload

    def _review_scope_invalid_result(
        self,
        session_id: str,
        observer_url: str,
        response_text: str,
        review_scope_errors: list[dict[str, object]],
        stats: dict[str, Any],
        metadata: dict[str, Any],
        budget: dict[str, Any],
        warnings: list[str],
        request_provenance: RequestProvenance,
        *,
        repaired: bool,
        repair_attempted: bool,
        repair_succeeded: bool,
    ) -> dict[str, Any]:
        payload = {
            "status": "invalid",
            "response_preview": _preview_text(response_text),
            "parse_error": None,
            "validation_errors": review_scope_errors,
            "review_scope_errors": review_scope_errors,
            "session_id": session_id,
            "run_id": session_id,
            "conversation_id": self.hub.get_session(session_id, raw=True).get("conversation_id"),
            "observer_url": observer_url,
            "stats": stats,
            "metadata": metadata,
            "budget": budget,
            "warnings": warnings,
            "repaired": repaired,
            "repair_attempted": repair_attempted,
            "repair_succeeded": repair_succeeded,
            **request_provenance.result_fields(),
        }
        self.hub.append_event(session_id, "review.scope_failed", "codex_mcp", {"validation_errors": review_scope_errors})
        self.hub.set_status(session_id, "invalid", "session.completed", {"result": payload})
        return payload

    def _repair_or_invalid(
        self,
        session_id: str,
        schema: dict[str, Any],
        raw_response: str,
        parse_error: str | None,
        validation_errors: list[dict[str, Any]],
        cwd: Path | None,
        *,
        request_provenance: RequestProvenance | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Any = None,
        heartbeat_callback: Any = None,
    ) -> dict[str, Any]:
        self.hub.set_status(
            session_id,
            "repairing",
            "repair.started",
            {"parse_error": parse_error, "validation_errors": validation_errors},
        )
        repair_prompt = _repair_prompt(schema, raw_response, parse_error, validation_errors)
        self.hub.append_event(
            session_id,
            "repair.prompt_sent",
            "codex_mcp",
            {"prompt_preview": _preview_text(repair_prompt), "prompt_chars": len(repair_prompt)},
            phase="repair",
        )
        runner_started = time.monotonic()
        result = self._call_runner(
            repair_prompt,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            phase="repair",
            cancel_event=cancel_event,
            process_callback=process_callback,
            heartbeat_callback=heartbeat_callback,
            heartbeat_interval_sec=self.config.agy_heartbeat_interval_sec,
        )
        runner_duration_ms = _elapsed_ms(runner_started)
        if result.status == "error":
            response_text, envelope = extract_cli_response(result.stdout)
            budget = build_budget(
                prompt=repair_prompt,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(result.metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=result.metadata,
            )
            return {"status": "error", "result": result, "budget": budget}
        if result.status in {"cancelled", "interrupted"}:
            response_text, envelope = extract_cli_response(result.stdout)
            budget = build_budget(
                prompt=repair_prompt,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=result.message,
                duration_ms=_metadata_duration_ms(result.metadata, runner_duration_ms),
                envelope=envelope,
                stats=result.stats,
                metadata=result.metadata,
            )
            return {"status": result.status, "result": result, "budget": budget}
        response_text, envelope = extract_cli_response(result.stdout)
        envelope_error = _envelope_error(envelope)
        stats = _merged_stats(result.stats, envelope)
        metadata = _metadata_with_provenance(_result_metadata(result, envelope), request_provenance) if request_provenance is not None else _result_metadata(result, envelope)
        budget = build_budget(
            prompt=repair_prompt,
            response=response_text,
            raw_stdout=result.raw_stdout or result.stdout,
            result=response_text,
            duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
            envelope=envelope,
            stats=stats,
            metadata=metadata,
        )
        if envelope_error:
            return {
                "status": "error",
                "result": AgyRunResult.error(envelope_error, exit_code=result.exit_code, stderr=result.stderr, stdout=result.stdout, metadata=metadata),
                "budget": budget,
            }
        self.hub.append_event(
            session_id,
            "repair.response",
            "gemness",
            {"response_preview": _preview_text(response_text), "response_chars": len(response_text), "budget": budget},
            phase="repair",
        )
        data, repair_parse_error, candidate = parse_json_candidate(response_text)
        if repair_parse_error is not None:
            self.hub.append_event(
                session_id,
                "repair.validation_failed",
                "codex_mcp",
                {"parse_error": repair_parse_error, "candidate": candidate},
                phase="repair",
            )
            return {"status": "invalid", "raw_response": response_text, "budget": budget}
        repair_validation_errors = validate_json_schema(data, schema)
        if repair_validation_errors:
            self.hub.append_event(
                session_id,
                "repair.validation_failed",
                "codex_mcp",
                {"validation_errors": repair_validation_errors, "candidate": candidate},
                phase="repair",
            )
            return {"status": "invalid", "raw_response": response_text, "budget": build_budget(
                prompt=repair_prompt,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=_json_result_text(data),
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=stats,
                metadata=metadata,
            )}
        self.hub.append_event(session_id, "repair.validation_passed", "codex_mcp", {"data": data}, phase="repair")
        return {
            "status": "valid",
            "data": data,
            "raw_response": response_text,
            "budget": build_budget(
                prompt=repair_prompt,
                response=response_text,
                raw_stdout=result.raw_stdout or result.stdout,
                result=_json_result_text(data),
                duration_ms=_metadata_duration_ms(metadata, runner_duration_ms),
                envelope=envelope,
                stats=stats,
                metadata=metadata,
            ),
        }

    def _retry_text(self, tool_name: str, parent_session_id: str, original_prompt: str, result: AgyRunResult, cwd: Path | None) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.raw_stdout or result.stdout, result.interrupt_instruction or "")
        return self._run_text_session(tool_name, prompt, parent_session_id=parent_session_id, cwd=cwd, title_source=result.interrupt_instruction or original_prompt)

    def _retry_json(self, tool_name: str, parent_session_id: str, original_prompt: str, result: AgyRunResult, schema: dict[str, Any], cwd: Path | None) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.raw_stdout or result.stdout, result.interrupt_instruction or "")
        return self._run_json_session(tool_name, prompt, schema, parent_session_id=parent_session_id, cwd=cwd, title_source=result.interrupt_instruction or original_prompt)

    def _runner_error(
        self,
        session_id: str,
        observer_url: str,
        result: AgyRunResult,
        *,
        budget: dict[str, Any] | None = None,
        request_provenance: RequestProvenance | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.hub.sessions.get(session_id)
        metadata = metadata if metadata is not None else result.metadata
        payload = {
            "status": "error",
            "exit_code": result.exit_code,
            "session_id": session_id,
            "run_id": session.run_id if session else session_id,
            "conversation_id": session.conversation_id if session else None,
            "observer_url": observer_url,
            "stderr_tail": result.stderr[-4000:] if result.stderr else "",
            "message": result.message,
            "stats": result.stats,
            "metadata": metadata,
        }
        if budget is not None:
            payload["budget"] = budget
        if request_provenance is not None:
            payload.update(request_provenance.result_fields())
        self.hub.set_status(session_id, "error", "session.error", payload)
        return payload

    def _envelope_error_result(
        self,
        session_id: str,
        observer_url: str,
        message: str,
        result: AgyRunResult,
        stats: dict[str, Any],
        metadata: dict[str, Any],
        *,
        budget: dict[str, Any] | None = None,
        request_provenance: RequestProvenance | None = None,
    ) -> dict[str, Any]:
        session = self.hub.sessions.get(session_id)
        payload = {
            "status": "error",
            "exit_code": result.exit_code,
            "session_id": session_id,
            "run_id": session.run_id if session else session_id,
            "conversation_id": session.conversation_id if session else None,
            "observer_url": observer_url,
            "stderr_tail": result.stderr[-4000:] if result.stderr else "",
            "message": f"Antigravity CLI envelope error: {message}",
            "stats": stats,
            "metadata": metadata,
        }
        if budget is not None:
            payload["budget"] = budget
        if request_provenance is not None:
            payload.update(request_provenance.result_fields())
        self.hub.set_status(session_id, "error", "session.error", payload)
        return payload

    def _session(
        self,
        tool_name: str,
        parent_session_id: str | None,
        existing_session_id: str | None,
        title: str | None = None,
        *,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        cwd: Path | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ):
        if existing_session_id is not None:
            return self.hub.sessions[existing_session_id]
        return self.hub.create_session(
            tool_name,
            DEFAULT_MODEL_LABEL,
            parent_session_id=parent_session_id,
            title=title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
            project_root=str(cwd) if cwd is not None else None,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def _follow_up_plan(self, parent_session_id: str, instruction: str) -> _FollowUpPlan:
        parent = self.hub.sessions[parent_session_id]
        cwd = _session_cwd(parent.project_root)
        if parent.project_root and cwd is None:
            fallback_reason = "project_root_unavailable"
        else:
            fallback_reason = None
        latest = self.hub.is_latest_run(parent_session_id)
        supports_conversation = self._native_conversation_supported(cwd)
        native_conversation_id = self._native_agy_conversation_id(parent) if supports_conversation else None
        if latest:
            root_run_id = self.hub.root_run_id(parent.conversation_id)
            if native_conversation_id:
                return _FollowUpPlan(
                    prompt=instruction,
                    conversation_id=parent.conversation_id,
                    parent_session_id=root_run_id if root_run_id != parent_session_id else parent_session_id,
                    parent_run_id=parent_session_id,
                    branch_from_conversation_id=None,
                    branch_from_run_id=None,
                    cwd=cwd,
                    native_conversation_id=native_conversation_id,
                    fallback_used=fallback_reason is not None,
                    fallback_reason=fallback_reason,
                )
            return _FollowUpPlan(
                prompt=self.hub.build_follow_up_prompt(parent_session_id, instruction),
                conversation_id=parent.conversation_id,
                parent_session_id=root_run_id if root_run_id != parent_session_id else parent_session_id,
                parent_run_id=parent_session_id,
                branch_from_conversation_id=None,
                branch_from_run_id=None,
                cwd=cwd,
                native_conversation_id=None,
                fallback_used=True,
                fallback_reason=fallback_reason
                or ("native_conversation_id_unavailable" if supports_conversation else "native_conversation_flag_unavailable"),
            )
        return _FollowUpPlan(
            prompt=self.hub.build_follow_up_prompt(parent_session_id, instruction),
            conversation_id=None,
            parent_session_id=parent_session_id,
            parent_run_id=parent_session_id,
            branch_from_conversation_id=parent.conversation_id,
            branch_from_run_id=parent_session_id,
            cwd=cwd,
            native_conversation_id=None,
            fallback_used=True,
            fallback_reason=fallback_reason or "branch_from_past_run",
        )

    def _with_allowed_plan_cwd(self, plan: _FollowUpPlan) -> _FollowUpPlan:
        resolved_cwd = resolve_workspace_cwd(self.config, str(plan.cwd) if plan.cwd is not None else None)
        return _FollowUpPlan(
            prompt=plan.prompt,
            conversation_id=plan.conversation_id,
            parent_session_id=plan.parent_session_id,
            parent_run_id=plan.parent_run_id,
            branch_from_conversation_id=plan.branch_from_conversation_id,
            branch_from_run_id=plan.branch_from_run_id,
            cwd=resolved_cwd,
            native_conversation_id=plan.native_conversation_id,
            fallback_used=plan.fallback_used,
            fallback_reason=plan.fallback_reason,
        )

    def _native_agy_conversation_id(self, parent) -> str | None:
        for value in (
            getattr(parent, "agy_conversation_id", None),
            self.hub.conversations.get(parent.conversation_id).current_agy_conversation_id
            if getattr(parent, "conversation_id", None) in self.hub.conversations
            else None,
        ):
            if isinstance(value, str) and _is_uuid(value):
                return value
        return None

    def _native_conversation_supported(self, cwd: Path | None) -> bool:
        probe_method = getattr(self.runner, "probe_capabilities", None)
        capabilities = probe_method(cwd) if callable(probe_method) else AgyCliRunner(self.config).probe_capabilities(cwd)
        return bool(capabilities.supports_conversation)

    def _conversation_lock(self, conversation_id: str | None) -> threading.Lock:
        key = conversation_id or "__new_conversation__"
        with self._conversation_locks_guard:
            lock = self._conversation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._conversation_locks[key] = lock
            return lock

    def _request_provenance(
        self,
        mode: str,
        prompt: str,
        cwd: Path | None,
        *,
        schema: dict[str, Any] | None = None,
        base_ref: str | None = None,
        parent_session_id: str | None = None,
        native_conversation_id: str | None = None,
    ) -> RequestProvenance:
        return build_request_provenance(
            mode=mode,
            prompt=prompt,
            cwd=cwd,
            schema=schema,
            base_ref=base_ref,
            parent_session_id=parent_session_id,
            native_conversation_id=native_conversation_id,
            auto_dedupe_enabled=self.config.enable_auto_dedupe,
        )

    def _record_request_provenance(self, session_id: str, provenance: RequestProvenance) -> None:
        events = self.hub.get_events(session_id, raw=True)
        if any(event.get("type") == "request.fingerprinted" for event in events):
            return
        self.hub.append_event(session_id, "request.fingerprinted", "system", provenance.event_payload())

def validate_base_ref(base_ref: str) -> str:
    ref = base_ref.strip()
    if not ref:
        raise ValueError("base_ref is required")
    if ref.startswith("-"):
        raise ValueError("base_ref must not start with '-'")
    if any(ch.isspace() or ord(ch) < 32 for ch in ref):
        raise ValueError("base_ref must not contain whitespace or control characters")
    if len(ref) > 200:
        raise ValueError("base_ref too long")
    return ref


def _session_title(prompt: str, tool_name: str, limit: int = 45) -> str:
    fallback = {
        "ask_antigravity": "Antigravity 질문",
        "ask_antigravity_json": "Antigravity JSON 질문",
        "review_current_diff_with_antigravity": "현재 변경 리뷰",
    }.get(tool_name, "Gemness 세션")
    for line in _title_candidate_lines(prompt):
        cleaned = _clean_title_line(line)
        if cleaned:
            return _clip_title(cleaned, limit)
    return fallback


def _title_candidate_lines(prompt: str) -> list[str]:
    lines = [line.strip() for line in str(prompt or "").splitlines()]
    candidates: list[str] = []
    marker_next = False
    for line in lines:
        if not line:
            continue
        normalized = line.rstrip(":：").strip()
        if marker_next:
            candidates.append(line)
            marker_next = False
            continue
        if normalized.lower() in {"codex", "user", "user request", "user follow-up", "사용자 요청", "후속 질문"}:
            marker_next = True
            continue
        if normalized.startswith(("Codex", "User", "사용자")) and normalized.endswith((":", "：")):
            marker_next = True
            continue
        candidates.append(line)
    return candidates


def _clean_title_line(line: str) -> str:
    text = line.strip().strip("\"'“”‘’`")
    text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", text)
    text = re.sub(r"^\s*(?:Codex|User|사용자|요청)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    lower = text.lower()
    skip_prefixes = (
        "observer에서",
        "비밀",
        "자격 증명",
        "return only",
        "json schema",
        "schema:",
        "previous session summary",
        "continue from this previous",
        "이전 antigravity 답변",
        "antigravity의 답변",
        "antigravity의 마지막 답변",
    )
    if lower.startswith(skip_prefixes):
        return ""
    return text


def _clip_title(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip(" ,.;:!?。．、，")
    return f"{clipped}..."


def _json_prompt(prompt: str, schema: dict[str, Any]) -> str:
    return (
        f"{prompt}\n\n"
        "Return only a JSON value that matches this JSON Schema. Do not include Markdown fences or prose.\n"
        "JSON Schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _repair_prompt(schema: dict[str, Any], raw_response: str, parse_error: str | None, validation_errors: list[dict[str, Any]] | None) -> str:
    return (
        "Repair only the previous Antigravity response so it conforms to the schema. Gemness is not resending "
        "the original task, repository materials, diffs, file dumps, logs, or transcript payloads. Do not solve "
        "the task again, do not add new analysis, and do not invent new facts. Preserve the meaning of the "
        "existing response as much as possible and return only the repaired JSON.\n\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Previous Antigravity response:\n{raw_response}\n\n"
        f"Parse error:\n{parse_error or 'none'}\n\n"
        f"Validation errors:\n{json.dumps(validation_errors or [], ensure_ascii=False, indent=2)}"
    )


def _interrupted_retry_prompt(original_prompt: str, _partial_response: str, instruction: str) -> str:
    return (
        "A previous headless Antigravity CLI subprocess was interrupted. Start a fresh answer with "
        "the retry instruction applied. Gemness is not forwarding the interrupted partial output; use the "
        "current working directory and Antigravity CLI's own tools as needed. Do not assume live injection "
        "into the old process or rely on omitted partial content.\n\n"
        f"Original prompt:\n{original_prompt}\n\n"
        f"Retry instruction:\n{instruction}\n\n"
        "Re-answer with the retry instruction applied."
    )


def _merged_stats(result_stats: dict[str, Any], envelope: dict[str, Any] | None) -> dict[str, Any]:
    stats = dict(result_stats)
    if isinstance(envelope, dict) and isinstance(envelope.get("stats"), dict):
        stats.update(envelope["stats"])
    if isinstance(envelope, dict) and isinstance(envelope.get("metadata"), dict):
        stats.setdefault("metadata", envelope["metadata"])
    return stats


def _metadata_with_provenance(metadata: dict[str, Any], provenance: RequestProvenance | None) -> dict[str, Any]:
    if provenance is None:
        return dict(metadata)
    return dict(metadata) | provenance.metadata_fields()


def _metadata_duration_ms(metadata: dict[str, Any], fallback_ms: int) -> int:
    value = metadata.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return fallback_ms


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _json_result_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_provenance_fields(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") != "request.fingerprinted":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        fields: dict[str, Any] = {}
        for key in ("request_fingerprint", "workspace_fingerprint", "workspace_fingerprint_degraded"):
            if key in payload:
                fields[key] = payload[key]
        return fields
    result = _event_result(events)
    if isinstance(result, dict):
        return {key: result[key] for key in ("request_fingerprint", "workspace_fingerprint", "workspace_fingerprint_degraded") if key in result}
    return {}


def _review_payload_fields(data: Any, review_workspace: ReviewWorkspace | None) -> dict[str, Any]:
    if review_workspace is None:
        return {}
    review_scope = data.get("review_scope") if isinstance(data, dict) else None
    return {
        "review_scope": review_scope,
        "expected_review_scope": review_workspace.to_payload(),
    }


def _mode_for_tool(tool_name: str) -> str:
    if tool_name == "ask_antigravity_json":
        return "json"
    if tool_name == "review_current_diff_with_antigravity":
        return "review_current_diff"
    return "ask"


def _result_metadata(result: AgyRunResult, envelope: dict[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(envelope, dict) and isinstance(envelope.get("metadata"), dict):
        metadata.update(envelope["metadata"])
    metadata.update(result.metadata)
    return metadata


def _envelope_error(envelope: dict[str, Any] | None) -> str | None:
    if not isinstance(envelope, dict) or "error" not in envelope:
        return None
    error = envelope.get("error")
    if not error:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or error.get("code")
        return json.dumps(error, ensure_ascii=False) if message is None else str(message)
    return str(error)


def _events_after_cursor(events: list[dict[str, Any]], event_cursor: str | None) -> list[dict[str, Any]]:
    if not event_cursor:
        return events
    for index, event in enumerate(events):
        if event.get("event_id") == event_cursor:
            return events[index + 1 :]
    return events


def _event_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        event_type = event.get("type")
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if event_type == "session.completed":
            result = payload.get("result")
            return result if isinstance(result, dict) else payload
        if event_type in {"session.error", "session.cancelled"}:
            return payload
    return None


def _clean_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    key = " ".join(str(idempotency_key).split())
    return key or None


def _idempotency_context(tool_name: str, cwd: Path | None, provenance: RequestProvenance, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "tool_name": tool_name,
        "cwd": str(cwd) if cwd is not None else None,
        "workspace_fingerprint": provenance.workspace_fingerprint,
        "workspace_fingerprint_degraded": provenance.workspace_fingerprint_degraded,
    }
    context.update(extra)
    return {key: value for key, value in context.items() if value is not None}


def _idempotency_lock_key(idempotency_key: str, idempotency_context: dict[str, Any] | None) -> str:
    if not idempotency_context:
        return idempotency_key
    payload = {"idempotency_key": idempotency_key, "context": idempotency_context}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"scoped:{hashlib.sha256(serialized.encode('utf-8', errors='replace')).hexdigest()}"


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-check-", dir=path, delete=True):
            pass
        return True
    except OSError:
        return False


def _is_git_repo(cwd: Path) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _session_cwd(project_root: str | None) -> Path | None:
    if not project_root:
        return None
    path = Path(project_root).expanduser()
    if not path.exists() or not path.is_dir():
        return None
    return path.resolve()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def clean_advisory_text(text: str) -> str:
    lines = str(text or "").splitlines()
    kept = [line for line in lines if not _is_progress_noise_line(line)]
    cleaned = "\n".join(kept).strip()
    return cleaned or str(text or "").strip()


def _is_progress_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in PROGRESS_NOISE_PATTERNS)


def _advisory_summary(text: str) -> str:
    stripped = " ".join(str(text or "").split())
    if len(stripped) <= 400:
        return stripped
    return stripped[:400].rstrip() + "..."


def _preview_text(text: str, limit: int = RESPONSE_PREVIEW_CHARS) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n...[truncated {len(value) - limit} chars]"


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _null_lock() -> _NullLock:
    return _NullLock()
