from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
            "available": command_exists(self.config.gemini_command),
        }
        if check_gemini:
            version, version_warning = _gemini_version(command_parts, resolved_cwd, self.config)
            gemini["version"] = version
            if version_warning:
                warnings.append(version_warning)

        workspace = {
            "cwd": str(resolved_cwd) if resolved_cwd is not None else None,
            "is_git_repo": _is_git_repo(resolved_cwd) if resolved_cwd is not None else False,
            "allowed": cwd_error is None,
            "workspace_root": str(Path(self.config.workspace_root).expanduser().resolve()) if self.config.workspace_root else None,
            "allowed_roots": [str(root) for root in normalized_allowed_roots(self.config)],
            "error": cwd_error,
        }
        observer = {
            "enabled": self.config.observer_enabled,
            "host": self.config.observer_host,
            "port": self.config.observer_port,
            "url": self.hub.base_url if self.config.observer_enabled else "",
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
        return self._run_text_session("ask_text", prompt, model or self.config.model, cwd=resolved_cwd)

    def ask_json(self, prompt: str, schema: dict[str, Any], model: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._run_json_session("ask_json", prompt, schema, model or self.config.model, cwd=resolved_cwd)

    def review_current_diff(self, base_ref: str = "HEAD", model: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        try:
            resolved_cwd = resolve_workspace_cwd(self.config, cwd)
            base_ref = validate_base_ref(base_ref)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        model = model or self.config.model
        session = self.hub.create_session("review_current_diff", model)
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
        )

    def follow_up(self, parent_session_id: str, instruction: str, model: str | None = None) -> dict[str, Any]:
        prompt = self.hub.build_follow_up_prompt(parent_session_id, instruction)
        return self._run_text_session("ask_text", prompt, model or self.config.model, parent_session_id=parent_session_id)

    def start_follow_up(self, parent_session_id: str, instruction: str, model: str | None = None) -> str:
        prompt = self.hub.build_follow_up_prompt(parent_session_id, instruction)
        session = self.hub.create_session("ask_text", model or self.config.model, parent_session_id=parent_session_id)

        def run() -> None:
            self._run_text_session(
                "ask_text",
                prompt,
                model or self.config.model,
                parent_session_id=parent_session_id,
                existing_session_id=session.session_id,
            )

        import threading

        threading.Thread(target=run, daemon=True).start()
        return session.session_id

    def _run_text_session(
        self,
        tool_name: str,
        prompt: str,
        model: str,
        *,
        parent_session_id: str | None = None,
        existing_session_id: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        session = self._session(tool_name, model, parent_session_id, existing_session_id)
        session_id = session.session_id
        observer_url = self.hub.observer_url(session_id)
        try:
            prompt_to_send = self.hub.prepare_prompt(session_id, prompt)
        except SessionCancelled as exc:
            return {"status": "cancelled", "session_id": session_id, "observer_url": observer_url, "message": str(exc)}

        result = self.runner.run(prompt_to_send, model=model, output_format="json", session_id=session_id, hub=self.hub, cwd=cwd)
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
    ) -> dict[str, Any]:
        session = self._session(tool_name, model, parent_session_id, existing_session_id)
        session_id = session.session_id
        observer_url = self.hub.observer_url(session_id)
        schema_error = validate_schema_definition(schema)
        if schema_error is not None:
            payload = {
                "status": "error",
                "session_id": session_id,
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

        result = self.runner.run(prompt_to_send, model=model, output_format="json", session_id=session_id, hub=self.hub, cwd=cwd)
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
        result = self.runner.run(prompt, model=model, output_format="json", session_id=session_id, hub=self.hub, cwd=cwd, phase="repair")
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
        return self._run_text_session(tool_name, prompt, model, parent_session_id=parent_session_id, cwd=cwd)

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
        return self._run_json_session(tool_name, prompt, schema, model, parent_session_id=parent_session_id, cwd=cwd)

    def _runner_error(self, session_id: str, observer_url: str, result: GeminiRunResult) -> dict[str, Any]:
        payload = {
            "status": "error",
            "exit_code": result.exit_code,
            "session_id": session_id,
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
        payload = {
            "status": "error",
            "exit_code": result.exit_code,
            "session_id": session_id,
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
    ):
        if existing_session_id is not None:
            return self.hub.sessions[existing_session_id]
        return self.hub.create_session(tool_name, model, parent_session_id=parent_session_id)

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
