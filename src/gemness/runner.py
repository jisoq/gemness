from __future__ import annotations

import os
import subprocess
import threading
import time
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import GemnessConfig
from .observer import ObserverHub

GEMINI_CLI_TRUST_WORKSPACE_ENV = "GEMINI_CLI_TRUST_WORKSPACE"


@dataclass(slots=True)
class GeminiRunResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    message: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    interrupt_instruction: str | None = None

    @classmethod
    def completed(cls, stdout: str, stderr: str = "", exit_code: int = 0, stats: dict[str, Any] | None = None) -> "GeminiRunResult":
        return cls(status="completed", stdout=stdout, stderr=stderr, exit_code=exit_code, stats=stats or {})

    @classmethod
    def error(cls, message: str, *, exit_code: int | None = None, stderr: str = "", stdout: str = "") -> "GeminiRunResult":
        return cls(status="error", stdout=stdout, stderr=stderr, exit_code=exit_code, message=message)

    @classmethod
    def cancelled(cls, stdout: str = "", stderr: str = "") -> "GeminiRunResult":
        return cls(status="cancelled", stdout=stdout, stderr=stderr, exit_code=None, message="Session cancelled")

    @classmethod
    def interrupted(cls, instruction: str, *, stdout: str = "", stderr: str = "") -> "GeminiRunResult":
        return cls(
            status="interrupted",
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
            message="Session interrupted for retry",
            interrupt_instruction=instruction,
        )


class GeminiRunner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        model: str,
        output_format: str,
        session_id: str,
        hub: ObserverHub,
        cwd: Path | None = None,
        phase: str | None = None,
    ) -> GeminiRunResult:
        ...


class GeminiCliRunner:
    def __init__(self, config: GemnessConfig) -> None:
        self.config = config

    def run(
        self,
        prompt: str,
        *,
        model: str,
        output_format: str,
        session_id: str,
        hub: ObserverHub,
        cwd: Path | None = None,
        phase: str | None = None,
    ) -> GeminiRunResult:
        command = resolve_gemini_command(self.config.gemini_command)
        command.extend(["-m", model, "--output-format", output_format])
        if self.config.gemini_skip_trust:
            command.append("--skip-trust")
        if self.config.gemini_approval_mode:
            command.extend(["--approval-mode", self.config.gemini_approval_mode])
        command.extend(["-p", prompt])
        env = gemness_env(self.config)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd) if cwd is not None else None,
                env=env,
            )
        except FileNotFoundError:
            return GeminiRunResult.error(f"Gemini CLI not found: {self.config.gemini_command}", exit_code=None)
        except OSError as exc:
            return GeminiRunResult.error(str(exc), exit_code=None)

        hub.set_status(
            session_id,
            "running",
            "gemini.started",
            {
                "model": model,
                "output_format": output_format,
                "pid": process.pid,
                "cwd": str(cwd) if cwd is not None else "",
                "trust_workspace": self.config.gemini_trust_workspace,
            },
            role="gemness",
            phase=phase,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_thread = _reader_thread(process.stdout, stdout_parts)
        stderr_thread = _reader_thread(process.stderr, stderr_parts)

        timed_out = False
        while process.poll() is None:
            intervention = hub.consume_running_intervention(session_id)
            if intervention is not None:
                _terminate_process(process)
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                stdout = "".join(stdout_parts)
                stderr = "".join(stderr_parts)
                hub.append_event(
                    session_id,
                    "gemini.exited",
                    "gemness",
                    {"exit_code": process.poll(), "interrupted": intervention.action},
                    phase=phase,
                )
                if intervention.action == "cancel":
                    return GeminiRunResult.cancelled(stdout=stdout, stderr=stderr)
                return GeminiRunResult.interrupted(
                    intervention.instruction or "Retry with the user intervention applied.",
                    stdout=stdout,
                    stderr=stderr,
                )
            if time.monotonic() - started > self.config.tool_timeout_sec:
                timed_out = True
                _terminate_process(process)
                break
            time.sleep(0.05)

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        exit_code = process.poll()

        if stdout:
            hub.append_event(session_id, "gemini.response", "gemness", {"response": stdout}, phase=phase)
        if stderr:
            hub.append_event(session_id, "gemini.stderr", "gemness", {"stderr": _tail(stderr)}, phase=phase)
        hub.append_event(
            session_id,
            "gemini.exited",
            "gemness",
            {"exit_code": exit_code, "duration_ms": int((time.monotonic() - started) * 1000)},
            phase=phase,
        )

        if timed_out:
            return GeminiRunResult.error("Gemini CLI timed out", exit_code=exit_code, stderr=stderr, stdout=stdout)
        if exit_code != 0:
            return GeminiRunResult.error("Gemini CLI exited with an error", exit_code=exit_code, stderr=stderr, stdout=stdout)
        return GeminiRunResult.completed(stdout=stdout, stderr=stderr, exit_code=exit_code or 0)


def _reader_thread(pipe: Any, parts: list[str]) -> threading.Thread:
    def read_pipe() -> None:
        if pipe is None:
            return
        try:
            value = pipe.read()
            if value:
                parts.append(value)
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    thread = threading.Thread(target=read_pipe, daemon=True)
    thread.start()
    return thread


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _tail(value: str, limit: int = 4000) -> str:
    return value[-limit:]


def gemness_env(config: GemnessConfig, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env[GEMINI_CLI_TRUST_WORKSPACE_ENV] = "true" if config.gemini_trust_workspace else "false"
    return env


def resolve_gemini_command(command: str) -> list[str]:
    resolved = shutil.which(command) or command
    path = Path(resolved)
    if path.name.lower() in {"gemini.cmd", "gemini.ps1", "gemini"}:
        npm_root = path.parent
        script = npm_root / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
        node = shutil.which("node")
        if node and script.exists():
            return [node, str(script)]
    return [resolved]


def command_exists(command: str) -> bool:
    parts = resolve_gemini_command(command)
    executable = parts[0] if parts else command
    if shutil.which(executable):
        return True
    return Path(executable).expanduser().exists()
