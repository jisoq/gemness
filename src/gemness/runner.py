from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import DEFAULT_AGY_COMMAND, DEFAULT_MODEL_LABEL, GemnessConfig
from .observer import ObserverHub


AUTH_REQUIRED_MARKERS = (
    "not logged in",
    "not authenticated",
    "please log in",
    "please login",
    "please sign in",
    "authentication required",
    "authorization required",
    "authorization code",
    "browser-based google sign-in",
)

WINPTY_ROWS = 48
WINPTY_COLS = 160
RESPONSE_PREVIEW_CHARS = 4000
CAPTURE_MODE_PIPE = "pipe"
CAPTURE_MODE_WINPTY = "winpty"
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_SINGLE_RE = re.compile(r"\x1b[@-Z\\-_]")


@dataclass(slots=True)
class AgyRunResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    message: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_stdout: str = ""
    interrupt_instruction: str | None = None

    @classmethod
    def completed(
        cls,
        stdout: str,
        stderr: str = "",
        exit_code: int = 0,
        stats: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        raw_stdout: str | None = None,
    ) -> "AgyRunResult":
        return cls(
            status="completed",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            stats=stats or {},
            metadata=metadata or {},
            raw_stdout=stdout if raw_stdout is None else raw_stdout,
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
        stdout: str = "",
        metadata: dict[str, Any] | None = None,
        raw_stdout: str | None = None,
    ) -> "AgyRunResult":
        return cls(
            status="error",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            message=message,
            metadata=metadata or {},
            raw_stdout=stdout if raw_stdout is None else raw_stdout,
        )

    @classmethod
    def cancelled(cls, stdout: str = "", stderr: str = "", metadata: dict[str, Any] | None = None) -> "AgyRunResult":
        return cls(status="cancelled", stdout=stdout, stderr=stderr, exit_code=None, message="Session cancelled", metadata=metadata or {})

    @classmethod
    def interrupted(
        cls,
        instruction: str,
        *,
        stdout: str = "",
        stderr: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "AgyRunResult":
        return cls(
            status="interrupted",
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
            message="Session interrupted for retry",
            metadata=metadata or {},
            raw_stdout=stdout,
            interrupt_instruction=instruction,
        )


@dataclass(slots=True)
class AgyCapabilities:
    command: list[str]
    available: bool
    resolved: str
    version: str | None = None
    help_text: str = ""
    print_flag: str | None = None
    supports_continue: bool = False
    supports_conversation: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def print_supported(self) -> bool:
        return self.print_flag is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "command": self.command,
            "resolved": self.resolved,
            "version": self.version,
            "print_mode": {"supported": self.print_supported, "flag": self.print_flag},
            "conversation_flags": {
                "continue": self.supports_continue,
                "conversation": self.supports_conversation,
            },
            "warnings": self.warnings,
            "error": self.error,
            "streaming": False,
        }


@dataclass(slots=True)
class AgyAuthProbe:
    status: str
    message: str
    exit_code: int | None = None
    stdout: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr_tail": self.stderr_tail,
        }


@dataclass(slots=True)
class _RunningAgyCommand:
    process: Any
    pid: int | None
    capture_mode: str
    stdout_parts: list[str]
    stderr_parts: list[str]
    stdout_byte_count: list[int]
    stderr_byte_count: list[int]
    capture_lock: threading.Lock
    stdout_thread: threading.Thread | None
    stderr_thread: threading.Thread | None
    poll_fn: Callable[[], int | None]
    terminate_fn: Callable[[], None]
    terminate_requested: bool = False

    def poll(self) -> int | None:
        return self.poll_fn()

    def terminate(self) -> None:
        if self.poll() is None:
            self.terminate_requested = True
        self.terminate_fn()

    def join(self) -> None:
        if self.stdout_thread is not None:
            self.stdout_thread.join(timeout=1)
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=1)

    def stdout(self) -> str:
        with self.capture_lock:
            value = "".join(self.stdout_parts)
        if self.capture_mode == CAPTURE_MODE_WINPTY:
            return clean_console_output(value)
        return value

    def stderr(self) -> str:
        with self.capture_lock:
            return "".join(self.stderr_parts)


@dataclass(slots=True)
class _CommandCapture:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    capture_mode: str
    error: str | None = None


class AgyRunner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        session_id: str,
        hub: ObserverHub,
        cwd: Path | None = None,
        phase: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        native_conversation_id: str | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[Any], None] | None = None,
        heartbeat_callback: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_sec: float | None = None,
    ) -> AgyRunResult:
        ...


class AgyCliRunner:
    def __init__(self, config: GemnessConfig) -> None:
        self.config = config
        self._capabilities: AgyCapabilities | None = None

    def run(
        self,
        prompt: str,
        *,
        session_id: str,
        hub: ObserverHub,
        cwd: Path | None = None,
        phase: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        native_conversation_id: str | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[Any], None] | None = None,
        heartbeat_callback: Callable[[dict[str, Any]], None] | None = None,
        heartbeat_interval_sec: float | None = None,
    ) -> AgyRunResult:
        capabilities = self.probe_capabilities(cwd)
        if not capabilities.available:
            return AgyRunResult.error(capabilities.error or f"Antigravity CLI not found: {self.config.agy_command}", exit_code=None)
        if not capabilities.print_flag:
            return AgyRunResult.error("Antigravity CLI print mode is not available; expected -p, --print, or --prompt.", exit_code=None)

        command, native_session_mode = _build_agy_command(
            capabilities,
            prompt,
            native_conversation_id=native_conversation_id,
        )
        recorded_command = _redact_prompt_argument(command, capabilities.print_flag)
        capture_mode = _resolve_capture_mode(self.config)
        hub.record_run_command(
            session_id,
            recorded_command,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            agy_conversation_id=native_conversation_id,
            native_session_mode=native_session_mode,
            model_requested=None,
            model_source="antigravity_cli_settings",
            phase=phase,
        )
        started = time.monotonic()
        env = gemness_env(self.config)
        try:
            running = _start_agy_command(command, cwd, env, capture_mode)
        except FileNotFoundError:
            return AgyRunResult.error(f"Antigravity CLI not found: {self.config.agy_command}", exit_code=None)
        except OSError as exc:
            return AgyRunResult.error(str(exc), exit_code=None)
        except RuntimeError as exc:
            return AgyRunResult.error(str(exc), exit_code=None)
        if process_callback is not None:
            process_callback(running)

        conversation_id = _conversation_id(hub, session_id)
        hub.set_status(
            session_id,
            "running",
            "antigravity.started",
            {
                "run_id": session_id,
                "conversation_id": conversation_id,
                "model": DEFAULT_MODEL_LABEL,
                "model_source": "antigravity_cli_settings",
                "print_flag": capabilities.print_flag,
                "pid": running.pid,
                "cwd": str(cwd) if cwd is not None else "",
                "capture_mode": running.capture_mode,
                "agy_conversation_id": native_conversation_id,
                "native_session_mode": native_session_mode,
                "streaming": False,
            },
            role="gemness",
            phase=phase,
        )

        timed_out = False
        cancelled = False
        last_stdout_bytes, last_stderr_bytes = _captured_bytes(running)
        last_activity_monotonic = started
        heartbeat_every = max(0.1, float(heartbeat_interval_sec or self.config.agy_heartbeat_interval_sec or 5.0))
        next_heartbeat = started + heartbeat_every
        while running.poll() is None:
            now = time.monotonic()
            stdout_bytes, stderr_bytes = _captured_bytes(running)
            if (stdout_bytes, stderr_bytes) != (last_stdout_bytes, last_stderr_bytes):
                last_stdout_bytes, last_stderr_bytes = stdout_bytes, stderr_bytes
                last_activity_monotonic = now
            if heartbeat_callback is not None and now >= next_heartbeat:
                heartbeat_callback(
                    {
                        "elapsed_ms": int((now - started) * 1000),
                        "timeout_remaining_ms": max(0, int((self.config.agy_timeout_sec - (now - started)) * 1000)),
                        "pid": running.pid,
                        "capture_mode": running.capture_mode,
                        "stdout_bytes": stdout_bytes,
                        "stderr_bytes": stderr_bytes,
                        "last_activity_ms_ago": int((now - last_activity_monotonic) * 1000),
                        "streaming": False,
                    }
                )
                next_heartbeat = now + heartbeat_every
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                hub.append_event(session_id, "antigravity.cancel_requested", "gemness", {"pid": running.pid, "phase": phase}, phase=phase)
                running.terminate()
                break
            if time.monotonic() - started > self.config.agy_timeout_sec:
                timed_out = True
                hub.append_event(
                    session_id,
                    "antigravity.timeout",
                    "gemness",
                    {"timeout_sec": self.config.agy_timeout_sec, "pid": running.pid, "phase": phase},
                    phase=phase,
                )
                running.terminate()
                break
            time.sleep(0.05)

        running.join()
        raw_stdout = running.stdout()
        stderr = running.stderr()
        exit_code = running.poll()
        if not cancelled and not timed_out and cancel_event is not None and cancel_event.is_set() and running.terminate_requested:
            cancelled = True
        auth_status = detect_auth_status(raw_stdout, stderr, exit_code)
        if exit_code == 0 and not raw_stdout.strip():
            auth_status = "unknown"
        metadata = _metadata(
            session_id=session_id,
            conversation_id=conversation_id,
            command=recorded_command,
            cwd=cwd,
            duration_ms=int((time.monotonic() - started) * 1000),
            exit_code=exit_code,
            auth_status=auth_status,
            capture_mode=running.capture_mode,
            agy_conversation_id=native_conversation_id,
            native_session_mode=native_session_mode,
        )
        if timed_out:
            metadata["timed_out"] = True
        if cancelled:
            metadata["cancelled"] = True
        envelope = _response_envelope(raw_stdout, metadata)
        if raw_stdout:
            stdout_artifact = hub.write_text_artifact(session_id, "stdout.txt" if phase is None else f"{phase}.stdout.txt", raw_stdout)
            hub.append_event(
                session_id,
                "antigravity.response",
                "gemness",
                {
                    "response_preview": _preview(raw_stdout),
                    "stdout_artifact": stdout_artifact,
                    "stdout_bytes": stdout_artifact["bytes"],
                    "metadata": metadata,
                    "streaming": False,
                },
                phase=phase,
            )
        if stderr:
            hub.append_event(session_id, "antigravity.stderr", "gemness", {"stderr": _tail(stderr)}, phase=phase)
        hub.append_event(session_id, "antigravity.exited", "gemness", metadata, phase=phase)

        if cancelled:
            return AgyRunResult.cancelled(stdout=envelope, stderr=stderr, metadata=metadata)
        if timed_out:
            return AgyRunResult.error("Antigravity CLI timed out", exit_code=exit_code, stderr=stderr, stdout=envelope, metadata=metadata, raw_stdout=raw_stdout)
        if auth_status == "auth_required":
            return AgyRunResult.error(
                "Antigravity CLI authentication is required. Run `agy` once and complete Google sign-in.",
                exit_code=exit_code,
                stderr=stderr,
                stdout=envelope,
                metadata=metadata,
                raw_stdout=raw_stdout,
            )
        if exit_code != 0:
            return AgyRunResult.error("Antigravity CLI exited with an error", exit_code=exit_code, stderr=stderr, stdout=envelope, metadata=metadata, raw_stdout=raw_stdout)
        if not raw_stdout.strip():
            return AgyRunResult.error("Antigravity CLI returned no output", exit_code=exit_code, stderr=stderr, stdout=envelope, metadata=metadata, raw_stdout=raw_stdout)
        return AgyRunResult.completed(envelope, stderr=stderr, exit_code=exit_code or 0, stats={"metadata": metadata}, metadata=metadata, raw_stdout=raw_stdout)

    def probe_capabilities(self, cwd: Path | None = None) -> AgyCapabilities:
        if self._capabilities is not None:
            return self._capabilities
        command = resolve_agy_command(self.config.agy_command)
        resolved = command[0] if command else self.config.agy_command
        if not _executable_exists(resolved):
            fallback_paths = [str(path) for path in agy_fallback_paths()]
            message = f"Antigravity CLI not found: {self.config.agy_command}"
            if fallback_paths:
                message += f"; checked fallback paths: {', '.join(fallback_paths)}"
            return AgyCapabilities(command=command, available=False, resolved=resolved, error=message)
        try:
            version_completed = subprocess.run(
                [*command, "--version"],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                env=gemness_env(self.config),
            )
            help_completed = subprocess.run(
                [*command, "--help"],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                env=gemness_env(self.config),
            )
        except FileNotFoundError:
            return AgyCapabilities(command=command, available=False, resolved=resolved, error=f"Antigravity CLI not found: {self.config.agy_command}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AgyCapabilities(command=command, available=False, resolved=resolved, error=f"Antigravity CLI capability probe failed: {exc}")

        help_text = "\n".join(part for part in [help_completed.stdout, help_completed.stderr] if part)
        version = (version_completed.stdout or version_completed.stderr).strip() or None
        warnings: list[str] = []
        if version_completed.returncode != 0:
            warnings.append(f"`agy --version` failed: {(version_completed.stderr or version_completed.stdout).strip() or version_completed.returncode}")
        if help_completed.returncode != 0:
            warnings.append(f"`agy --help` failed: {(help_completed.stderr or help_completed.stdout).strip() or help_completed.returncode}")
        print_flag = _select_print_flag(help_text)
        if print_flag is None:
            warnings.append("Antigravity CLI help does not advertise -p, --print, or --prompt.")
        capabilities = AgyCapabilities(
            command=command,
            available=help_completed.returncode == 0,
            resolved=resolved,
            version=version,
            help_text=help_text,
            print_flag=print_flag,
            supports_continue="--continue" in help_text,
            supports_conversation="--conversation" in help_text,
            warnings=warnings,
            error=None if help_completed.returncode == 0 else "Antigravity CLI help check failed",
        )
        if capabilities.available and capabilities.print_flag:
            self._capabilities = capabilities
        return capabilities


def probe_auth(command: list[str], print_flag: str | None, cwd: Path | None, config: GemnessConfig) -> AgyAuthProbe:
    if not command:
        return AgyAuthProbe("not_checked", "Antigravity CLI command is empty")
    if print_flag is None:
        return AgyAuthProbe("not_checked", "Antigravity CLI print mode is not available")
    prompt = "Return exactly: GEMNESS_AGY_HEALTHCHECK"
    captured = _capture_agy_command(
        [*command, print_flag, prompt],
        cwd=cwd,
        env=gemness_env(config),
        timeout=config.agy_health_timeout_sec,
        capture_mode=_resolve_capture_mode(config),
    )
    if captured.error:
        if "not found" in captured.error.lower():
            return AgyAuthProbe("not_checked", captured.error)
        return AgyAuthProbe("unknown", captured.error)
    if captured.timed_out:
        return AgyAuthProbe("unknown", "Antigravity CLI auth probe timed out")

    stdout = captured.stdout.strip()
    stderr = captured.stderr.strip()
    auth_status = detect_auth_status(stdout, stderr, captured.exit_code)
    if auth_status == "auth_required":
        return AgyAuthProbe("auth_required", "Antigravity CLI requires Google sign-in.", captured.exit_code, stdout, _tail(stderr))
    if captured.exit_code != 0:
        return AgyAuthProbe("unknown", f"Antigravity CLI auth probe exited with {captured.exit_code}.", captured.exit_code, stdout, _tail(stderr))
    if "GEMNESS_AGY_HEALTHCHECK" in stdout:
        return AgyAuthProbe("authenticated", "Antigravity CLI print mode returned the expected health-check text.", captured.exit_code, stdout, _tail(stderr))
    if not stdout:
        return AgyAuthProbe("unknown", "Antigravity CLI print mode returned no output.", captured.exit_code, stdout, _tail(stderr))
    return AgyAuthProbe("unknown", "Antigravity CLI print mode returned unexpected output.", captured.exit_code, stdout, _tail(stderr))


def _capture_agy_command(
    command: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    timeout: float,
    capture_mode: str,
) -> _CommandCapture:
    try:
        running = _start_agy_command(command, cwd, env, capture_mode)
    except FileNotFoundError:
        return _CommandCapture("", "", None, False, capture_mode, f"Antigravity CLI not found: {command[0]}")
    except (OSError, RuntimeError) as exc:
        return _CommandCapture("", "", None, False, capture_mode, f"Antigravity CLI auth probe failed: {exc}")

    started = time.monotonic()
    timed_out = False
    while running.poll() is None:
        if time.monotonic() - started > timeout:
            timed_out = True
            running.terminate()
            break
        time.sleep(0.05)
    running.join()
    return _CommandCapture(running.stdout(), running.stderr(), running.poll(), timed_out, running.capture_mode)


def _start_agy_command(command: list[str], cwd: Path | None, env: dict[str, str], capture_mode: str) -> _RunningAgyCommand:
    if capture_mode == CAPTURE_MODE_WINPTY:
        return _start_winpty_command(command, cwd, env)
    return _start_pipe_command(command, cwd, env)


def _start_pipe_command(command: list[str], cwd: Path | None, env: dict[str, str]) -> _RunningAgyCommand:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_byte_count = [0]
    stderr_byte_count = [0]
    capture_lock = threading.Lock()
    stdout_thread = _reader_thread(process.stdout, stdout_parts, stdout_byte_count, capture_lock)
    stderr_thread = _reader_thread(process.stderr, stderr_parts, stderr_byte_count, capture_lock)
    return _RunningAgyCommand(
        process=process,
        pid=process.pid,
        capture_mode=CAPTURE_MODE_PIPE,
        stdout_parts=stdout_parts,
        stderr_parts=stderr_parts,
        stdout_byte_count=stdout_byte_count,
        stderr_byte_count=stderr_byte_count,
        capture_lock=capture_lock,
        stdout_thread=stdout_thread,
        stderr_thread=stderr_thread,
        poll_fn=process.poll,
        terminate_fn=lambda: _terminate_process(process),
    )


def _start_winpty_command(command: list[str], cwd: Path | None, env: dict[str, str]) -> _RunningAgyCommand:
    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise RuntimeError("Windows console capture requires pywinpty. Install Gemness with Windows dependencies or set GEMNESS_AGY_CAPTURE_MODE=pipe.") from exc

    process = PtyProcess.spawn(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        dimensions=(WINPTY_ROWS, WINPTY_COLS),
    )
    stdout_parts: list[str] = []
    stdout_byte_count = [0]
    stderr_byte_count = [0]
    capture_lock = threading.Lock()
    stdout_thread = _winpty_reader_thread(process, stdout_parts, stdout_byte_count, capture_lock)

    def poll() -> int | None:
        if process.isalive():
            return None
        try:
            return process.exitstatus
        except OSError:
            return None

    return _RunningAgyCommand(
        process=process,
        pid=getattr(process, "pid", None),
        capture_mode=CAPTURE_MODE_WINPTY,
        stdout_parts=stdout_parts,
        stderr_parts=[],
        stdout_byte_count=stdout_byte_count,
        stderr_byte_count=stderr_byte_count,
        capture_lock=capture_lock,
        stdout_thread=stdout_thread,
        stderr_thread=None,
        poll_fn=poll,
        terminate_fn=lambda: _terminate_winpty_process(process),
    )


def _reader_thread(pipe: Any, parts: list[str], byte_count: list[int], lock: threading.Lock) -> threading.Thread:
    def read_pipe() -> None:
        if pipe is None:
            return
        try:
            while True:
                value = pipe.readline()
                if value == "":
                    break
                with lock:
                    parts.append(value)
                    byte_count[0] += len(value.encode("utf-8", errors="replace"))
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    thread = threading.Thread(target=read_pipe, daemon=True)
    thread.start()
    return thread


def _winpty_reader_thread(process: Any, parts: list[str], byte_count: list[int], lock: threading.Lock) -> threading.Thread:
    def read_console() -> None:
        while True:
            try:
                value = process.read(4096)
            except EOFError:
                break
            except OSError:
                break
            if not value:
                if not process.isalive():
                    break
                time.sleep(0.05)
                continue
            with lock:
                parts.append(value)
                byte_count[0] += len(value.encode("utf-8", errors="replace"))

    thread = threading.Thread(target=read_console, daemon=True)
    thread.start()
    return thread


def _captured_bytes(running: _RunningAgyCommand) -> tuple[int, int]:
    with running.capture_lock:
        return running.stdout_byte_count[0], running.stderr_byte_count[0]


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        _terminate_windows_process_tree(process)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=2)


def _terminate_windows_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _terminate_winpty_process(process: Any) -> None:
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
        process.wait()
    except Exception:
        try:
            process.close()
        except Exception:
            pass


def _tail(value: str, limit: int = 4000) -> str:
    return value[-limit:]


def _preview(value: str, limit: int = RESPONSE_PREVIEW_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n...[truncated {len(value) - limit} chars]"


def clean_console_output(value: str) -> str:
    value = _OSC_RE.sub("", value)
    value = _ANSI_CSI_RE.sub("", value)
    value = _ANSI_SINGLE_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.lstrip("\n").strip()


def _resolve_capture_mode(config: GemnessConfig) -> str:
    mode = config.agy_capture_mode
    if mode == CAPTURE_MODE_PIPE:
        return CAPTURE_MODE_PIPE
    if mode == CAPTURE_MODE_WINPTY:
        return CAPTURE_MODE_WINPTY
    if os.name == "nt" and _winpty_available():
        return CAPTURE_MODE_WINPTY
    return CAPTURE_MODE_PIPE


def _winpty_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


def _select_print_flag(help_text: str) -> str | None:
    if re.search(r"(^|\s)-p(\s|,|$)", help_text):
        return "-p"
    for flag in ("--print", "--prompt"):
        if flag in help_text:
            return flag
    return None


def _build_agy_command(
    capabilities: AgyCapabilities,
    prompt: str,
    *,
    native_conversation_id: str | None,
) -> tuple[list[str], str]:
    command = [*capabilities.command]
    if native_conversation_id and capabilities.supports_conversation:
        command.extend(["--conversation", native_conversation_id])
        native_session_mode = "conversation"
    else:
        native_session_mode = "new"
    command.extend([capabilities.print_flag or "-p", prompt])
    return command, native_session_mode


def _redact_prompt_argument(command: list[str], print_flag: str | None) -> list[str]:
    safe: list[str] = []
    redact_next = False
    prompt_flags = {"-p", "--print", "--prompt"}
    if print_flag:
        prompt_flags.add(print_flag)
    for item in command:
        if redact_next:
            safe.append("[PROMPT_REDACTED]")
            redact_next = False
            continue
        safe.append(item)
        if item in prompt_flags:
            redact_next = True
    return safe


def _response_envelope(raw_stdout: str, metadata: dict[str, Any]) -> str:
    parsed = _json_object(raw_stdout)
    if parsed is not None and _has_response_text(parsed):
        envelope = dict(parsed)
        envelope_metadata = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
        envelope["metadata"] = {**envelope_metadata, **metadata}
        return json.dumps(envelope, ensure_ascii=False)
    return json.dumps({"response": raw_stdout, "metadata": metadata}, ensure_ascii=False)


def _json_object(value: str) -> dict[str, Any] | None:
    stripped = value.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_response_text(value: dict[str, Any]) -> bool:
    return any(isinstance(value.get(key), str) for key in ("response", "text", "content", "output"))


def _metadata(
    *,
    session_id: str,
    conversation_id: str | None,
    command: list[str],
    cwd: Path | None,
    duration_ms: int,
    exit_code: int | None,
    auth_status: str,
    capture_mode: str,
    agy_conversation_id: str | None,
    native_session_mode: str,
) -> dict[str, Any]:
    return {
        "run_id": session_id,
        "conversation_id": conversation_id,
        "agy_conversation_id": agy_conversation_id,
        "native_session_mode": native_session_mode,
        "command": command,
        "cwd": str(cwd) if cwd is not None else None,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "auth_status": auth_status,
        "capture_mode": capture_mode,
        "streaming": False,
    }


def _conversation_id(hub: ObserverHub, session_id: str) -> str | None:
    session = hub.sessions.get(session_id)
    return session.conversation_id if session is not None else None


def detect_auth_status(stdout: str, stderr: str, exit_code: int | None) -> str:
    if exit_code == 0 and stdout.strip():
        return "ok"
    diagnostic = stderr.strip().lower()
    if exit_code != 0 and not diagnostic:
        diagnostic = stdout.strip().lower()
    if any(marker in diagnostic for marker in AUTH_REQUIRED_MARKERS):
        return "auth_required"
    if exit_code == 0:
        return "ok"
    return "unknown"


def gemness_env(config: GemnessConfig, base_env: dict[str, str] | None = None) -> dict[str, str]:
    return dict(base_env or os.environ)


def agy_fallback_paths() -> list[Path]:
    paths: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        paths.append(Path(local_app_data) / "agy" / "bin" / "agy.exe")
    return paths


def resolve_agy_command(command: str | None = None) -> list[str]:
    raw_command = (command or DEFAULT_AGY_COMMAND).strip() or DEFAULT_AGY_COMMAND
    expanded = Path(raw_command).expanduser()
    if expanded.exists():
        return [str(expanded)]
    resolved = shutil.which(raw_command)
    if resolved:
        return [resolved]
    if raw_command == DEFAULT_AGY_COMMAND:
        for path in agy_fallback_paths():
            if path.exists():
                return [str(path)]
    return [raw_command]


def command_exists(command: str | None = None) -> bool:
    parts = resolve_agy_command(command)
    executable = parts[0] if parts else command or DEFAULT_AGY_COMMAND
    return _executable_exists(executable)


def _executable_exists(executable: str) -> bool:
    if shutil.which(executable):
        return True
    return Path(executable).expanduser().exists()
