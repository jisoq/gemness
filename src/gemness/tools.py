from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import GemnessConfig
from .json_utils import extract_cli_response, parse_json_candidate
from .mcp_metadata import SERVER_NAME, SERVER_VERSION, TOOL_NAMES
from .observer import ObserverHub, SessionCancelled
from .review import REVIEW_SCHEMA, build_review_prompt
from .runner import GeminiCliRunner, GeminiRunner, GeminiRunResult, command_exists, gemness_env, resolve_gemini_command
from .schema_validation import validate_json_schema, validate_schema_definition
from .workspace import normalized_allowed_roots, resolve_workspace_cwd

DiffProvider = Callable[[str, Path], str]


class _NativeResumeRequiredError(RuntimeError):
    pass


@dataclass(slots=True)
class _FollowUpPlan:
    prompt: str
    conversation_id: str | None
    parent_session_id: str | None
    parent_run_id: str | None
    branch_from_conversation_id: str | None
    branch_from_run_id: str | None
    cwd: Path | None
    native_session_mode: str
    native_resume_enabled: bool
    fallback_used: bool
    fallback_reason: str | None


class GemnessService:
    def __init__(
        self,
        config: GemnessConfig | None = None,
        *,
        hub: ObserverHub | None = None,
        runner: GeminiRunner | None = None,
        diff_provider: DiffProvider | None = None,
    ) -> None:
        self.config = config or GemnessConfig.from_env()
        self.hub = hub or ObserverHub(self.config)
        self.hub.attach_service(self)
        self.runner = runner or GeminiCliRunner(self.config)
        self.diff_provider = diff_provider or self._git_diff
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._conversation_locks_guard = threading.RLock()
        if self.config.observer_enabled and self.config.observer_start_on_init:
            self.hub.start_web_server()

    def shutdown(self) -> None:
        self.hub.shutdown()

    def health_check(self, *, cwd: str | None = None, check_gemini: bool = True) -> dict[str, Any]:
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

        command_parts = resolve_gemini_command(self.config.gemini_command)
        gemini: dict[str, Any] = {
            "command": self.config.gemini_command,
            "resolved": command_parts[0] if command_parts else self.config.gemini_command,
            "argv": command_parts,
            "model": self.config.model,
            "version": None,
            "skip_trust": self.config.gemini_skip_trust,
            "trust_workspace": self.config.gemini_trust_workspace,
            "approval_mode": self.config.gemini_approval_mode,
            "output_format": self.config.gemini_output_format,
            "available": command_exists(self.config.gemini_command),
        }
        if check_gemini:
            version, version_warning = _gemini_version(command_parts, resolved_cwd, self.config)
            gemini["version"] = version
            if version_warning:
                warnings.append(version_warning)
            native_probe = self._native_resume_probe(resolved_cwd)
            gemini["native_resume"] = {
                "mode": self.config.gemini_native_resume,
                "supported": native_probe.get("supported"),
                "reason": native_probe.get("reason"),
                "missing": native_probe.get("missing", []),
            }

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
            "gemini": gemini,
            "observer": observer,
            "transcript": {"dir": str(transcript_dir), "writable": transcript_writable},
            "warnings": warnings,
        }

    def ask_text(self, prompt: str, model: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._run_text_session("ask_text", prompt, model or self.config.model, cwd=resolved_cwd, title_source=prompt)

    def ask_json(self, prompt: str, schema: dict[str, Any], model: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._run_json_session("ask_json", prompt, schema, model or self.config.model, cwd=resolved_cwd, title_source=prompt)

    def review_current_diff(self, base_ref: str = "HEAD", model: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
            base_ref = validate_base_ref(base_ref)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        model = model or self.config.model
        try:
            native_resume_enabled, _native_reason = self._native_resume_available(resolved_cwd)
        except _NativeResumeRequiredError as exc:
            session = self.hub.create_session(
                "review_current_diff",
                model,
                title=f"현재 diff 리뷰: {base_ref}",
                project_root=str(resolved_cwd),
                native_resume_enabled=False,
            )
            return self._runner_error(session.session_id, self.hub.observer_url(session.session_id), GeminiRunResult.error(str(exc), exit_code=None))
        session = self.hub.create_session(
            "review_current_diff",
            model,
            title=f"현재 diff 리뷰: {base_ref}",
            project_root=str(resolved_cwd),
            native_resume_enabled=native_resume_enabled,
        )
        try:
            diff = self.diff_provider(base_ref, resolved_cwd)
        except Exception as exc:  # noqa: BLE001 - tool result should carry process errors.
            self.hub.set_status(session.session_id, "error", "session.error", {"message": str(exc)})
            return {
                "status": "error",
                "exit_code": None,
                "session_id": session.session_id,
                "observer_url": self.hub.observer_url(session.session_id),
                "message": str(exc),
            }

        warnings: list[str] = []
        diff_bytes = diff.encode("utf-8")
        if len(diff_bytes) > self.config.diff_limit_bytes:
            diff = diff_bytes[: self.config.diff_limit_bytes].decode("utf-8", errors="replace")
            warnings.append(f"Diff truncated to {self.config.diff_limit_bytes} bytes")
        prompt = build_review_prompt(diff, base_ref)
        return self._run_json_session(
            "review_current_diff",
            prompt,
            REVIEW_SCHEMA,
            model,
            existing_session_id=session.session_id,
            warnings=warnings,
            cwd=resolved_cwd,
            title_source=f"현재 diff 리뷰: {base_ref}",
        )

    def follow_up(self, parent_session_id: str, instruction: str, model: str | None = None) -> dict[str, Any]:
        self.hub.refresh_from_disk()
        if parent_session_id not in self.hub.sessions:
            return {"status": "error", "message": f"Unknown parent_session_id: {parent_session_id}"}
        plan = self._follow_up_plan(parent_session_id, instruction)
        lock = self._conversation_lock(plan.conversation_id) if plan.conversation_id else _null_lock()
        with lock:
            if self.hub.is_latest_run(parent_session_id):
                plan = self._follow_up_plan(parent_session_id, instruction)
            gemini_session_id = _new_gemini_session_id() if plan.fallback_reason == "session_rotation" else None
            session = self.hub.create_session(
                "ask_text",
                model or self.config.model,
                parent_session_id=plan.parent_session_id,
                title=_session_title(instruction, "ask_text"),
                conversation_id=plan.conversation_id,
                parent_run_id=plan.parent_run_id,
                branch_from_conversation_id=plan.branch_from_conversation_id,
                branch_from_run_id=plan.branch_from_run_id,
                project_root=str(plan.cwd) if plan.cwd is not None else None,
                gemini_session_id=gemini_session_id,
                native_resume_enabled=plan.native_resume_enabled,
                native_resume_used=plan.native_session_mode == "resume",
                fallback_used=plan.fallback_used,
                fallback_reason=plan.fallback_reason,
            )
            return self._run_text_session(
                "ask_text",
                plan.prompt,
                model or self.config.model,
                parent_session_id=plan.parent_session_id,
                existing_session_id=session.session_id,
                cwd=plan.cwd,
                title_source=instruction,
                native_session_mode=plan.native_session_mode,
                fallback_used=plan.fallback_used,
                fallback_reason=plan.fallback_reason,
                resume_fallback_parent_run_id=plan.parent_run_id,
            )

    def start_follow_up(self, parent_session_id: str, instruction: str, model: str | None = None) -> str:
        plan = self._follow_up_plan(parent_session_id, instruction)
        gemini_session_id = _new_gemini_session_id() if plan.fallback_reason == "session_rotation" else None
        session = self.hub.create_session(
            "ask_text",
            model or self.config.model,
            parent_session_id=plan.parent_session_id,
            title=_session_title(instruction, "ask_text"),
            conversation_id=plan.conversation_id,
            parent_run_id=plan.parent_run_id,
            branch_from_conversation_id=plan.branch_from_conversation_id,
            branch_from_run_id=plan.branch_from_run_id,
            project_root=str(plan.cwd) if plan.cwd is not None else None,
            gemini_session_id=gemini_session_id,
            native_resume_enabled=plan.native_resume_enabled,
            native_resume_used=plan.native_session_mode == "resume",
            fallback_used=plan.fallback_used,
            fallback_reason=plan.fallback_reason,
        )
        lock = self._conversation_lock(session.conversation_id)

        def run() -> None:
            with lock:
                self._run_text_session(
                    "ask_text",
                    plan.prompt,
                    model or self.config.model,
                    parent_session_id=plan.parent_session_id,
                    existing_session_id=session.session_id,
                    cwd=plan.cwd,
                    title_source=instruction,
                    native_session_mode=plan.native_session_mode,
                    fallback_used=plan.fallback_used,
                    fallback_reason=plan.fallback_reason,
                    resume_fallback_parent_run_id=plan.parent_run_id,
                )

        threading.Thread(target=run, daemon=True).start()
        return session.session_id

    def _legacy_follow_up(self, parent_session_id: str, instruction: str, model: str | None = None) -> dict[str, Any]:
        prompt = self.hub.build_follow_up_prompt(parent_session_id, instruction)
        return self._run_text_session(
            "ask_text",
            prompt,
            model or self.config.model,
            parent_session_id=parent_session_id,
            title_source=instruction,
        )

    def _run_text_session(
        self,
        tool_name: str,
        prompt: str,
        model: str,
        *,
        parent_session_id: str | None = None,
        existing_session_id: str | None = None,
        cwd: Path | None = None,
        title_source: str | None = None,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        native_session_mode: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        resume_fallback_parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        title = _session_title(title_source or prompt, tool_name)
        try:
            native_resume_enabled, native_unavailable_reason = self._native_resume_available(cwd)
        except _NativeResumeRequiredError as exc:
            session = self._session(
                tool_name,
                model,
                parent_session_id,
                existing_session_id,
                title,
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
                branch_from_conversation_id=branch_from_conversation_id,
                branch_from_run_id=branch_from_run_id,
                cwd=cwd,
                native_resume_enabled=False,
                native_resume_used=False,
                fallback_used=False,
                fallback_reason=None,
            )
            observer_url = self.hub.observer_url(session.session_id)
            return self._runner_error(session.session_id, observer_url, GeminiRunResult.error(str(exc), exit_code=None))
        if native_session_mode is None:
            native_session_mode = "start" if native_resume_enabled else "none"
        if native_unavailable_reason and native_session_mode == "none":
            fallback_reason = fallback_reason or native_unavailable_reason
        session = self._session(
            tool_name,
            model,
            parent_session_id,
            existing_session_id,
            title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
            cwd=cwd,
            native_resume_enabled=native_resume_enabled,
            native_resume_used=native_session_mode == "resume",
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        session_id = session.session_id
        if not session.title:
            self.hub.set_title(session_id, title)
        observer_url = self.hub.observer_url(session_id)
        try:
            prompt_to_send = self.hub.prepare_prompt(session_id, prompt)
        except SessionCancelled as exc:
            return {"status": "cancelled", "session_id": session_id, "observer_url": observer_url, "message": str(exc)}

        result = self.runner.run(
            prompt_to_send,
            model=model,
            output_format=self.config.gemini_output_format,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            gemini_session_id=session.gemini_session_id if native_session_mode != "none" else None,
            native_session_mode=native_session_mode,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        if result.status == "error" and native_session_mode == "resume" and resume_fallback_parent_run_id and _should_fallback_from_resume_error(result):
            fallback_prompt = self.hub.build_follow_up_prompt(resume_fallback_parent_run_id, prompt)
            rotated_session_id = _new_gemini_session_id()
            self.hub.rotate_gemini_session(session_id, rotated_session_id, "resume_failed")
            result = self.runner.run(
                fallback_prompt,
                model=model,
                output_format=self.config.gemini_output_format,
                session_id=session_id,
                hub=self.hub,
                cwd=cwd,
                gemini_session_id=rotated_session_id if native_resume_enabled else None,
                native_session_mode="start" if native_resume_enabled else "none",
                fallback_used=True,
                fallback_reason="resume_failed",
            )
            fallback_used = True
            fallback_reason = "resume_failed"
        if result.status == "interrupted":
            retry_result = self._retry_text(tool_name, session_id, prompt_to_send, result, model, cwd)
            self.hub.set_status(
                session_id,
                "cancelled",
                "session.cancelled",
                {"reason": "interrupted_and_retried", "child_session_id": retry_result.get("session_id")},
            )
            return retry_result
        if result.status == "cancelled":
            self.hub.set_status(session_id, "cancelled", "session.cancelled", {"reason": "user_cancelled"})
            return {"status": "cancelled", "session_id": session_id, "observer_url": observer_url}
        if result.status == "error":
            return self._runner_error(session_id, observer_url, result)

        text, envelope = extract_cli_response(result.stdout)
        stats = _merged_stats(result.stats, envelope)
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats)
        payload = {
            "status": "completed",
            "text": text,
            "session_id": session_id,
            "run_id": session.run_id or session_id,
            "conversation_id": session.conversation_id,
            "observer_url": observer_url,
            "stats": stats,
        }
        if envelope is not None:
            payload["stats"] = stats | {"cli_envelope_keys": sorted(envelope.keys())}
        self.hub.set_status(session_id, "completed", "session.completed", {"result": payload})
        return payload

    def _run_json_session(
        self,
        tool_name: str,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        *,
        parent_session_id: str | None = None,
        existing_session_id: str | None = None,
        warnings: list[str] | None = None,
        cwd: Path | None = None,
        title_source: str | None = None,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        native_session_mode: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        title = _session_title(title_source or prompt, tool_name)
        try:
            native_resume_enabled, native_unavailable_reason = self._native_resume_available(cwd)
        except _NativeResumeRequiredError as exc:
            session = self._session(
                tool_name,
                model,
                parent_session_id,
                existing_session_id,
                title,
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
                branch_from_conversation_id=branch_from_conversation_id,
                branch_from_run_id=branch_from_run_id,
                cwd=cwd,
                native_resume_enabled=False,
                native_resume_used=False,
                fallback_used=False,
                fallback_reason=None,
            )
            observer_url = self.hub.observer_url(session.session_id)
            return self._runner_error(session.session_id, observer_url, GeminiRunResult.error(str(exc), exit_code=None))
        if native_session_mode is None:
            native_session_mode = "start" if native_resume_enabled else "none"
        if native_unavailable_reason and native_session_mode == "none":
            fallback_reason = fallback_reason or native_unavailable_reason
        session = self._session(
            tool_name,
            model,
            parent_session_id,
            existing_session_id,
            title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
            cwd=cwd,
            native_resume_enabled=native_resume_enabled,
            native_resume_used=native_session_mode == "resume",
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        session_id = session.session_id
        if not session.title:
            self.hub.set_title(session_id, title)
        observer_url = self.hub.observer_url(session_id)
        schema_error = validate_schema_definition(schema)
        if schema_error is not None:
            payload = {
                "status": "error",
                "session_id": session_id,
                "run_id": session.run_id or session_id,
                "conversation_id": session.conversation_id,
                "observer_url": observer_url,
                "message": f"Invalid JSON Schema: {schema_error}",
            }
            self.hub.set_status(session_id, "error", "session.error", payload)
            return payload
        full_prompt = _json_prompt(prompt, schema)
        try:
            prompt_to_send = self.hub.prepare_prompt(session_id, full_prompt)
        except SessionCancelled as exc:
            return {"status": "cancelled", "session_id": session_id, "observer_url": observer_url, "message": str(exc)}

        result = self.runner.run(
            prompt_to_send,
            model=model,
            output_format=self.config.gemini_output_format,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            gemini_session_id=session.gemini_session_id if native_session_mode != "none" else None,
            native_session_mode=native_session_mode,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        if result.status == "interrupted":
            retry_result = self._retry_json(tool_name, session_id, prompt_to_send, result, schema, model, cwd)
            self.hub.set_status(
                session_id,
                "cancelled",
                "session.cancelled",
                {"reason": "interrupted_and_retried", "child_session_id": retry_result.get("session_id")},
            )
            return retry_result
        if result.status == "cancelled":
            self.hub.set_status(session_id, "cancelled", "session.cancelled", {"reason": "user_cancelled"})
            return {"status": "cancelled", "session_id": session_id, "observer_url": observer_url}
        if result.status == "error":
            return self._runner_error(session_id, observer_url, result)

        response_text, envelope = extract_cli_response(result.stdout)
        stats = _merged_stats(result.stats, envelope)
        envelope_error = _envelope_error(envelope)
        self.hub.append_event(
            session_id,
            "json.extracted",
            "codex_mcp",
            {"response": response_text, "cli_envelope": envelope, "stats": stats, "error": envelope_error},
        )
        if envelope_error:
            return self._envelope_error_result(session_id, observer_url, envelope_error, result, stats)
        data, parse_error, candidate = parse_json_candidate(response_text)
        if parse_error is not None:
            self.hub.append_event(session_id, "json.parse_failed", "codex_mcp", {"parse_error": parse_error, "candidate": candidate})
            return self._repair_or_invalid(
                session_id,
                observer_url,
                prompt_to_send,
                schema,
                response_text,
                stats,
                parse_error=parse_error,
                validation_errors=None,
                warnings=warnings,
                model=model,
                cwd=cwd,
            )

        validation_errors = validate_json_schema(data, schema)
        if validation_errors:
            self.hub.append_event(
                session_id,
                "json.validation_failed",
                "codex_mcp",
                {"validation_errors": validation_errors, "candidate": candidate},
            )
            return self._repair_or_invalid(
                session_id,
                observer_url,
                prompt_to_send,
                schema,
                response_text,
                stats,
                parse_error=None,
                validation_errors=validation_errors,
                warnings=warnings,
                model=model,
                cwd=cwd,
            )

        self.hub.append_event(session_id, "json.validation_passed", "codex_mcp", {"data": data})
        payload: dict[str, Any] = {
            "status": "valid",
            "data": data,
            "raw_response": response_text,
            "repaired": False,
            "repair_attempted": False,
            "repair_succeeded": False,
            "session_id": session_id,
            "run_id": session.run_id or session_id,
            "conversation_id": session.conversation_id,
            "observer_url": observer_url,
            "stats": stats,
        }
        if warnings:
            payload["warnings"] = warnings
        self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
        return payload

    def _repair_or_invalid(
        self,
        session_id: str,
        observer_url: str,
        original_prompt: str,
        schema: dict[str, Any],
        raw_response: str,
        stats: dict[str, Any],
        *,
        parse_error: str | None,
        validation_errors: list[dict[str, Any]] | None,
        warnings: list[str] | None,
        model: str,
        cwd: Path | None,
    ) -> dict[str, Any]:
        session = self.hub.sessions.get(session_id)
        repaired = self._repair_json_once(
            session_id,
            original_prompt,
            schema,
            raw_response,
            parse_error=parse_error,
            validation_errors=validation_errors,
            model=model,
            cwd=cwd,
        )
        if repaired["status"] == "valid":
            payload: dict[str, Any] = {
                "status": "valid",
                "data": repaired["data"],
                "raw_response": raw_response,
                "repaired": True,
                "repair_attempted": True,
                "repair_succeeded": True,
                "session_id": session_id,
                "run_id": session.run_id if session else session_id,
                "conversation_id": session.conversation_id if session else None,
                "observer_url": observer_url,
                "stats": stats,
                "repair_raw_response": repaired["raw_response"],
            }
            if warnings:
                payload["warnings"] = warnings
            self.hub.set_status(session_id, "valid", "session.completed", {"result": payload})
            return payload

        if repaired["status"] == "error":
            result = repaired["result"]
            return self._runner_error(session_id, observer_url, result)

        payload = {
            "status": "invalid",
            "raw_response": raw_response,
            "repaired": False,
            "repair_attempted": True,
            "repair_succeeded": False,
            "session_id": session_id,
            "run_id": session.run_id if session else session_id,
            "conversation_id": session.conversation_id if session else None,
            "observer_url": observer_url,
            "stats": stats,
            "repair_raw_response": repaired.get("raw_response"),
        }
        if parse_error:
            payload["parse_error"] = parse_error
        if validation_errors:
            payload["validation_errors"] = validation_errors
        self.hub.set_status(session_id, "invalid", "session.completed", {"result": payload})
        return payload

    def _repair_json_once(
        self,
        session_id: str,
        original_prompt: str,
        schema: dict[str, Any],
        raw_response: str,
        *,
        parse_error: str | None,
        validation_errors: list[dict[str, Any]] | None,
        model: str,
        cwd: Path | None,
    ) -> dict[str, Any]:
        self.hub.set_status(
            session_id,
            "repairing",
            "repair.started",
            {"parse_error": parse_error, "validation_errors": validation_errors},
        )
        prompt = _repair_prompt(original_prompt, schema, raw_response, parse_error, validation_errors)
        self.hub.append_event(session_id, "repair.prompt_sent", "codex_mcp", {"prompt": prompt}, phase="repair")
        result = self.runner.run(
            prompt,
            model=model,
            output_format=self.config.gemini_output_format,
            session_id=session_id,
            hub=self.hub,
            cwd=cwd,
            phase="repair",
            gemini_session_id=self.hub.sessions[session_id].gemini_session_id,
            native_session_mode="resume" if self.hub.sessions[session_id].native_resume_enabled else "none",
        )
        if result.status == "error":
            return {"status": "error", "result": result}
        if result.status in {"cancelled", "interrupted"}:
            return {"status": "invalid", "raw_response": result.stdout}

        response_text, envelope = extract_cli_response(result.stdout)
        stats = _merged_stats(result.stats, envelope)
        envelope_error = _envelope_error(envelope)
        if envelope_error:
            return {"status": "error", "result": GeminiRunResult.error(envelope_error, exit_code=result.exit_code, stderr=result.stderr, stdout=result.stdout)}
        self.hub.append_event(session_id, "repair.response", "gemness", {"response": response_text}, phase="repair")
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
        validation_errors = validate_json_schema(data, schema)
        if validation_errors:
            self.hub.append_event(
                session_id,
                "repair.validation_failed",
                "codex_mcp",
                {"validation_errors": validation_errors, "candidate": candidate},
                phase="repair",
            )
            return {"status": "invalid", "raw_response": response_text}
        self.hub.append_event(session_id, "repair.validation_passed", "codex_mcp", {"data": data}, phase="repair")
        return {"status": "valid", "data": data, "raw_response": response_text}

    def _retry_text(
        self,
        tool_name: str,
        parent_session_id: str,
        original_prompt: str,
        result: GeminiRunResult,
        model: str,
        cwd: Path | None,
    ) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.stdout, result.interrupt_instruction or "")
        return self._run_text_session(
            tool_name,
            prompt,
            model,
            parent_session_id=parent_session_id,
            cwd=cwd,
            title_source=result.interrupt_instruction or original_prompt,
        )

    def _retry_json(
        self,
        tool_name: str,
        parent_session_id: str,
        original_prompt: str,
        result: GeminiRunResult,
        schema: dict[str, Any],
        model: str,
        cwd: Path | None,
    ) -> dict[str, Any]:
        prompt = _interrupted_retry_prompt(original_prompt, result.stdout, result.interrupt_instruction or "")
        return self._run_json_session(
            tool_name,
            prompt,
            schema,
            model,
            parent_session_id=parent_session_id,
            cwd=cwd,
            title_source=result.interrupt_instruction or original_prompt,
        )

    def _runner_error(self, session_id: str, observer_url: str, result: GeminiRunResult) -> dict[str, Any]:
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
        }
        self.hub.set_status(session_id, "error", "session.error", payload)
        return payload

    def _envelope_error_result(
        self,
        session_id: str,
        observer_url: str,
        message: str,
        result: GeminiRunResult,
        stats: dict[str, Any],
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
            "message": f"Gemini CLI envelope error: {message}",
            "stats": stats,
        }
        self.hub.set_status(session_id, "error", "session.error", payload)
        return payload

    def _session(
        self,
        tool_name: str,
        model: str,
        parent_session_id: str | None,
        existing_session_id: str | None,
        title: str | None = None,
        *,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        cwd: Path | None = None,
        native_resume_enabled: bool = True,
        native_resume_used: bool = False,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ):
        if existing_session_id is not None:
            return self.hub.sessions[existing_session_id]
        return self.hub.create_session(
            tool_name,
            model,
            parent_session_id=parent_session_id,
            title=title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
            project_root=str(cwd) if cwd is not None else None,
            native_resume_enabled=native_resume_enabled,
            native_resume_used=native_resume_used,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def _follow_up_plan(self, parent_session_id: str, instruction: str) -> _FollowUpPlan:
        parent = self.hub.sessions[parent_session_id]
        cwd = _session_cwd(parent.project_root)
        if parent.project_root and cwd is None:
            if self.config.gemini_native_resume == "on":
                raise _NativeResumeRequiredError(f"Stored project_root is unavailable: {parent.project_root}")
            native_resume_enabled, native_reason = False, "project_root_unavailable"
        else:
            native_resume_enabled, native_reason = self._native_resume_available(cwd)
        latest = self.hub.is_latest_run(parent_session_id)
        rotate = (
            latest
            and native_resume_enabled
            and parent.conversation_id is not None
            and self.hub.conversations[parent.conversation_id].turn_count >= self.config.gemini_native_resume_max_turns
        )
        if latest and not rotate:
            prompt = instruction if native_resume_enabled else self.hub.build_follow_up_prompt(parent_session_id, instruction)
            fallback_used = not native_resume_enabled and native_reason is not None
            root_run_id = self.hub.root_run_id(parent.conversation_id)
            return _FollowUpPlan(
                prompt=prompt,
                conversation_id=parent.conversation_id,
                parent_session_id=root_run_id if root_run_id != parent_session_id else parent_session_id,
                parent_run_id=parent_session_id,
                branch_from_conversation_id=None,
                branch_from_run_id=None,
                cwd=cwd,
                native_session_mode="resume" if native_resume_enabled else "none",
                native_resume_enabled=native_resume_enabled,
                fallback_used=fallback_used,
                fallback_reason=native_reason if fallback_used else None,
            )

        fallback_reason = "session_rotation" if rotate else "branch_from_past_run"
        if rotate and parent.conversation_id:
            self.hub.update_conversation_summary(
                parent.conversation_id,
                self.hub.summarize_conversation(parent.conversation_id, through_run_id=parent_session_id),
            )
        prompt = self.hub.build_follow_up_prompt(parent_session_id, instruction)
        if rotate:
            return _FollowUpPlan(
                prompt=prompt,
                conversation_id=parent.conversation_id,
                parent_session_id=self.hub.root_run_id(parent.conversation_id) or parent_session_id,
                parent_run_id=parent_session_id,
                branch_from_conversation_id=None,
                branch_from_run_id=None,
                cwd=cwd,
                native_session_mode="start" if native_resume_enabled else "none",
                native_resume_enabled=native_resume_enabled,
                fallback_used=True,
                fallback_reason=fallback_reason,
            )
        return _FollowUpPlan(
            prompt=prompt,
            conversation_id=None,
            parent_session_id=parent_session_id,
            parent_run_id=parent_session_id,
            branch_from_conversation_id=parent.conversation_id,
            branch_from_run_id=parent_session_id,
            cwd=cwd,
            native_session_mode="start" if native_resume_enabled else "none",
            native_resume_enabled=native_resume_enabled,
            fallback_used=True,
            fallback_reason=fallback_reason,
        )

    def _native_resume_available(self, cwd: Path | None) -> tuple[bool, str | None]:
        if self.config.gemini_native_resume == "off":
            return False, "disabled_by_config"
        probe_method = getattr(self.runner, "probe_native_resume", None)
        if callable(probe_method):
            probe = probe_method(cwd)
            supported = bool(getattr(probe, "supported", False))
            reason = getattr(probe, "reason", None)
            if supported:
                return True, None
            if self.config.gemini_native_resume == "on":
                raise _NativeResumeRequiredError(str(reason or "Gemini CLI native resume is not supported"))
            return False, str(reason or "native_resume_unavailable")
        return True, None

    def _native_resume_probe(self, cwd: Path | None) -> dict[str, Any]:
        if self.config.gemini_native_resume == "off":
            return {"supported": False, "reason": "disabled_by_config", "missing": []}
        probe_method = getattr(self.runner, "probe_native_resume", None)
        if not callable(probe_method):
            return {"supported": True, "reason": None, "missing": []}
        probe = probe_method(cwd)
        return {
            "supported": bool(getattr(probe, "supported", False)),
            "reason": getattr(probe, "reason", None),
            "missing": list(getattr(probe, "missing", [])),
        }

    def _conversation_lock(self, conversation_id: str | None) -> threading.Lock:
        key = conversation_id or "__new_conversation__"
        with self._conversation_locks_guard:
            lock = self._conversation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._conversation_locks[key] = lock
            return lock

    def _git_diff(self, base_ref: str, cwd: Path) -> str:
        completed = subprocess.run(
            ["git", "diff", "--no-color", base_ref, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "git diff failed")
        return completed.stdout


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
        "ask_text": "텍스트 질문",
        "ask_json": "JSON 질문",
        "review_current_diff": "현재 diff 리뷰",
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
        "이전 gemini 답변",
        "gemini의 답변",
        "gemini의 마지막 답변",
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


def _repair_prompt(
    original_prompt: str,
    schema: dict[str, Any],
    raw_response: str,
    parse_error: str | None,
    validation_errors: list[dict[str, Any]] | None,
) -> str:
    return (
        "Repair the previous Gemini response so it conforms to the schema. Do not solve the task again, "
        "do not add new analysis, and do not invent new facts. Preserve the meaning of the existing "
        "response as much as possible and return only the repaired JSON.\n\n"
        f"Original prompt:\n{original_prompt}\n\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Previous response:\n{raw_response}\n\n"
        f"Parse error:\n{parse_error or 'none'}\n\n"
        f"Validation errors:\n{json.dumps(validation_errors or [], ensure_ascii=False, indent=2)}"
    )


def _interrupted_retry_prompt(original_prompt: str, partial_response: str, instruction: str) -> str:
    return (
        "A previous headless Gemini subprocess was interrupted by the user. Continue by re-answering "
        "with the intervention applied. Do not assume live injection into the old process.\n\n"
        f"Original prompt:\n{original_prompt}\n\n"
        f"Partial Gemini output, if any:\n{partial_response or '(none)'}\n\n"
        f"User intervention:\n{instruction}\n\n"
        "Re-answer with the intervention applied."
    )


def _merged_stats(result_stats: dict[str, Any], envelope: dict[str, Any] | None) -> dict[str, Any]:
    stats = dict(result_stats)
    if isinstance(envelope, dict) and isinstance(envelope.get("stats"), dict):
        stats.update(envelope["stats"])
    return stats


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


def _new_gemini_session_id() -> str:
    return f"gemness_{uuid.uuid4()}"


def _should_fallback_from_resume_error(result: GeminiRunResult) -> bool:
    text = f"{result.message}\n{result.stderr}\n{result.stdout}".lower()
    markers = ["resume", "session", "context", "maxsessionturns", "invalid session", "not found", "expired"]
    return any(marker in text for marker in markers)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _null_lock() -> _NullLock:
    return _NullLock()


def _gemini_version(command_parts: list[str], cwd: Path | None, config: GemnessConfig) -> tuple[str | None, str | None]:
    if not command_parts:
        return None, "Gemini CLI command is empty"
    executable = command_parts[0]
    if not command_exists(executable) and not Path(executable).expanduser().exists():
        return None, f"Gemini CLI not found: {executable}"
    try:
        completed = subprocess.run(
            [*command_parts, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=gemness_env(config),
        )
    except FileNotFoundError:
        return None, f"Gemini CLI not found: {executable}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Gemini CLI version check failed: {exc}"
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return None, f"Gemini CLI version check failed: {output or completed.returncode}"
    return output, None
