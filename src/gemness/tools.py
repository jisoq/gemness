from __future__ import annotations

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

from .config import DEFAULT_MODEL_LABEL, GemnessConfig
from .json_utils import extract_cli_response, parse_json_candidate
from .mcp_metadata import SERVER_NAME, SERVER_VERSION, TOOL_NAMES
from .observer import FINAL_STATUSES, ObserverHub
from .review import REVIEW_SCHEMA, build_review_prompt
from .run_manager import RunManager
from .runner import AgyCliRunner, AgyRunResult, AgyRunner, agy_fallback_paths, command_exists, probe_auth, resolve_agy_command
from .schema_validation import validate_json_schema, validate_schema_definition
from .workspace import normalized_allowed_roots, resolve_workspace_cwd


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
        self._idempotency_start_lock = threading.RLock()
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._conversation_locks_guard = threading.RLock()
        if self.config.observer_enabled and self.config.observer_start_on_init:
            self.hub.start_web_server()

    def shutdown(self) -> None:
        self.run_manager.shutdown()
        self.hub.shutdown()

    def antigravity_health(self, *, cwd: str | None = None, check_antigravity: bool = True) -> dict[str, Any]:
        warnings: list[str] = []
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
            cwd_error: str | None = None
        except ValueError as exc:
            resolved_cwd = None
            cwd_error = str(exc)
            warnings.append(cwd_error)

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
        if check_antigravity:
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
        antigravity["auth"] = auth_probe

        workspace = {
            "cwd": str(resolved_cwd) if resolved_cwd is not None else None,
            "is_git_repo": _is_git_repo(resolved_cwd) if resolved_cwd is not None else False,
            "allowed": cwd_error is None,
            "workspace_root": str(Path(self.config.workspace_root).expanduser().resolve()) if self.config.workspace_root else None,
            "allowed_roots": [str(root) for root in normalized_allowed_roots(self.config)],
            "error": cwd_error,
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
        status = "error" if cwd_error else "warning" if warnings else "ok"
        return {
            "status": status,
            "server": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "python": sys.version.split()[0],
                "executable": sys.executable,
            },
            "mcp": {"transport": "stdio", "tools": TOOL_NAMES},
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
        with self._idempotency_start_lock:
            existing = self._existing_idempotent_run(idempotency_key)
            if existing is not None:
                return existing
            try:
                resolved_cwd = resolve_workspace_cwd(self.config, cwd)
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            session = self._session("ask_antigravity", None, None, _session_title(prompt, "ask_antigravity"), cwd=resolved_cwd)
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_text_session(
                    "ask_antigravity",
                    prompt,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=prompt,
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
            )
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def start_antigravity_json(self, prompt: str, schema: dict[str, Any], cwd: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._idempotency_start_lock:
            existing = self._existing_idempotent_run(idempotency_key)
            if existing is not None:
                return existing
            schema_error = validate_schema_definition(schema)
            if schema_error:
                return {"status": "error", "message": f"Invalid JSON Schema: {schema_error}"}
            try:
                resolved_cwd = resolve_workspace_cwd(self.config, cwd)
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            session = self._session("ask_antigravity_json", None, None, _session_title(prompt, "ask_antigravity_json"), cwd=resolved_cwd)
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_json_session(
                    "ask_antigravity_json",
                    prompt,
                    schema,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=prompt,
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
            )
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def start_review_current_diff_with_antigravity(self, base_ref: str = "HEAD", cwd: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._idempotency_start_lock:
            existing = self._existing_idempotent_run(idempotency_key)
            if existing is not None:
                return existing
            try:
                resolved_cwd = resolve_workspace_cwd(self.config, cwd)
                base_ref = validate_base_ref(base_ref)
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            session = self.hub.create_session(
                "review_current_diff_with_antigravity",
                DEFAULT_MODEL_LABEL,
                title=f"현재 변경 리뷰: {base_ref}",
                project_root=str(resolved_cwd),
            )
            prompt = build_review_prompt(base_ref)
            self.run_manager.start(
                session.session_id,
                lambda cancel_event, process_callback, heartbeat_callback: self._run_json_session(
                    "review_current_diff_with_antigravity",
                    prompt,
                    REVIEW_SCHEMA,
                    existing_session_id=session.session_id,
                    cwd=resolved_cwd,
                    title_source=f"현재 변경 리뷰: {base_ref}",
                    cancel_event=cancel_event,
                    process_callback=process_callback,
                    heartbeat_callback=heartbeat_callback,
                ),
                idempotency_key=idempotency_key,
            )
            return self._start_payload(session.session_id, idempotency_key=idempotency_key)

    def start_follow_up_antigravity(self, parent_session_id: str, instruction: str, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._idempotency_start_lock:
            existing = self._existing_idempotent_run(idempotency_key)
            if existing is not None:
                return existing
            self.hub.refresh_from_disk()
            if parent_session_id not in self.hub.sessions:
                return {"status": "error", "message": f"Unknown parent_session_id: {parent_session_id}"}
            plan = self._follow_up_plan(parent_session_id, instruction)
            create_lock = self._conversation_lock(plan.conversation_id) if plan.conversation_id else _null_lock()
            with create_lock:
                if self.hub.is_latest_run(parent_session_id):
                    plan = self._follow_up_plan(parent_session_id, instruction)
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

    def _existing_idempotent_run(self, idempotency_key: str | None) -> dict[str, Any] | None:
        existing_run_id = self.run_manager.find_by_idempotency_key(idempotency_key)
        if existing_run_id is None:
            return None
        return self._run_status_payload(existing_run_id) | {"idempotent": True}

    def _start_payload(self, run_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        session = self.hub.get_session(run_id, raw=True)
        status = str(session["status"])
        result = _event_result(self.hub.get_events(run_id, raw=True)) if status in FINAL_STATUSES else None
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
        if result is not None:
            payload["result"] = result
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
        prompt_to_send = self.hub.prepare_prompt(session_id, prompt)

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
            payload = {"status": "cancelled", "session_id": session_id, "run_id": session.run_id or session_id, "conversation_id": session.conversation_id, "observer_url": observer_url, "metadata": result.metadata}
            self.hub.set_status(session_id, "cancelled", "session.cancelled", payload | {"reason": "user_cancelled"})
            return payload
        if result.status == "error":
            return self._runner_error(session_id, observer_url, result)

        text, envelope = extract_cli_response(result.stdout)
        clean_text = clean_advisory_text(text)
        stats = _merged_stats(result.stats, envelope)
        metadata = _result_metadata(result, envelope)
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats, metadata)
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
        prompt_to_send = self.hub.prepare_prompt(session_id, _json_prompt(prompt, schema))

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
            payload = {"status": "cancelled", "session_id": session_id, "run_id": session.run_id or session_id, "conversation_id": session.conversation_id, "observer_url": observer_url, "metadata": result.metadata}
            self.hub.set_status(session_id, "cancelled", "session.cancelled", payload | {"reason": "user_cancelled"})
            return payload
        if result.status == "error":
            return self._runner_error(session_id, observer_url, result)

        response_text, envelope = extract_cli_response(result.stdout)
        stats = _merged_stats(result.stats, envelope)
        metadata = _result_metadata(result, envelope)
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats, metadata)
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
                "warnings": warnings or [],
                "repaired": False,
                "repair_attempted": False,
                "repair_succeeded": False,
            }
            self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
            return payload
        repair = self._repair_or_invalid(
            session_id,
            schema,
            response_text,
            parse_error,
            validation_errors,
            cwd,
            cancel_event=cancel_event,
            process_callback=process_callback,
            heartbeat_callback=heartbeat_callback,
        )
        if repair["status"] == "valid":
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
                "warnings": warnings or [],
                "repaired": True,
                "repair_attempted": True,
                "repair_succeeded": True,
            }
            self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
            return payload
        if repair["status"] == "error":
            return self._runner_error(session_id, observer_url, repair["result"])
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
                "metadata": repair["result"].metadata,
                "repaired": False,
                "repair_attempted": True,
                "repair_succeeded": False,
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
            "warnings": warnings or [],
            "repaired": False,
            "repair_attempted": True,
            "repair_succeeded": False,
        }
        event_name = "json.parse_failed" if parse_error else "json.validation_failed"
        self.hub.append_event(session_id, event_name, "codex_mcp", {"parse_error": parse_error, "validation_errors": validation_errors})
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
        if result.status == "error":
            return {"status": "error", "result": result}
        if result.status in {"cancelled", "interrupted"}:
            return {"status": result.status, "result": result}
        response_text, envelope = extract_cli_response(result.stdout)
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return {"status": "error", "result": AgyRunResult.error(envelope_error, exit_code=result.exit_code, stderr=result.stderr, stdout=result.stdout, metadata=result.metadata)}
        self.hub.append_event(
            session_id,
            "repair.response",
            "gemness",
            {"response_preview": _preview_text(response_text), "response_chars": len(response_text)},
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
            return {"status": "invalid", "raw_response": response_text}
        repair_validation_errors = validate_json_schema(data, schema)
        if repair_validation_errors:
            self.hub.append_event(
                session_id,
                "repair.validation_failed",
                "codex_mcp",
                {"validation_errors": repair_validation_errors, "candidate": candidate},
                phase="repair",
            )
            return {"status": "invalid", "raw_response": response_text}
        self.hub.append_event(session_id, "repair.validation_passed", "codex_mcp", {"data": data}, phase="repair")
        return {"status": "valid", "data": data, "raw_response": response_text}

    def _retry_text(self, tool_name: str, parent_session_id: str, original_prompt: str, result: AgyRunResult, cwd: Path | None) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.raw_stdout or result.stdout, result.interrupt_instruction or "")
        return self._run_text_session(tool_name, prompt, parent_session_id=parent_session_id, cwd=cwd, title_source=result.interrupt_instruction or original_prompt)

    def _retry_json(self, tool_name: str, parent_session_id: str, original_prompt: str, result: AgyRunResult, schema: dict[str, Any], cwd: Path | None) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.raw_stdout or result.stdout, result.interrupt_instruction or "")
        return self._run_json_session(tool_name, prompt, schema, parent_session_id=parent_session_id, cwd=cwd, title_source=result.interrupt_instruction or original_prompt)

    def _runner_error(self, session_id: str, observer_url: str, result: AgyRunResult) -> dict[str, Any]:
        session = self.hub.sessions.get(session_id)
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
            "metadata": result.metadata,
        }
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


def _result_metadata(result: AgyRunResult, envelope: dict[str, Any] | None) -> dict[str, Any]:
    if result.metadata:
        return dict(result.metadata)
    if isinstance(envelope, dict) and isinstance(envelope.get("metadata"), dict):
        return dict(envelope["metadata"])
    return {}


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
