from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OBSERVER_PORT = 56755
DEFAULT_TRANSCRIPT_DIR = Path.home() / ".gemness" / "transcripts"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _trust_workspace_env() -> bool:
    value = os.getenv("GEMNESS_GEMINI_TRUST_WORKSPACE")
    if value is None:
        value = os.getenv("GEMINI_CLI_TRUST_WORKSPACE")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return True


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def _optional_path_env(name: str) -> Path | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return Path(value).expanduser()


def _path_list_env(name: str) -> tuple[Path, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return ()
    parts: list[str] = []
    for item in value.split(os.pathsep):
        parts.extend(piece.strip() for piece in item.split(","))
    return tuple(Path(item).expanduser() for item in parts if item)


def _loopback_host(value: str) -> str:
    host = value.strip() or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Gemness observer host must be loopback-only: 127.0.0.1, localhost, or ::1")
    return host


@dataclass(slots=True)
class GemnessConfig:
    model: str = os.getenv("GEMNESS_MODEL", "gemini-3.1-pro-preview")
    observer_enabled: bool = _bool_env("GEMNESS_OBSERVER_ENABLED", True)
    observer_host: str = _loopback_host(os.getenv("GEMNESS_OBSERVER_HOST", "127.0.0.1"))
    observer_port: int = _int_env("GEMNESS_OBSERVER_PORT", DEFAULT_OBSERVER_PORT)
    observer_start_on_init: bool = _bool_env("GEMNESS_OBSERVER_START_ON_INIT", True)
    transcript_dir: Path = Path(os.getenv("GEMNESS_TRANSCRIPT_DIR", str(DEFAULT_TRANSCRIPT_DIR))).expanduser()
    redact_raw_by_default: bool = _bool_env("GEMNESS_REDACT_RAW_BY_DEFAULT", True)
    pause_before_send: bool = _bool_env("GEMNESS_PAUSE_BEFORE_SEND", False)
    approval_timeout_sec: float = float(os.getenv("GEMNESS_APPROVAL_TIMEOUT_SEC", "300"))
    gemini_command: str = os.getenv("GEMNESS_COMMAND", "gemini")
    gemini_output_format: str = os.getenv("GEMNESS_GEMINI_OUTPUT_FORMAT", "stream-json")
    gemini_native_resume: str = _choice_env("GEMNESS_GEMINI_NATIVE_RESUME", "auto", {"auto", "on", "off"})
    gemini_native_resume_max_turns: int = _int_env("GEMNESS_GEMINI_NATIVE_RESUME_MAX_TURNS", 40)
    gemini_skip_trust: bool = _bool_env("GEMNESS_GEMINI_SKIP_TRUST", False)
    gemini_trust_workspace: bool = field(default_factory=_trust_workspace_env)
    gemini_approval_mode: str = os.getenv("GEMNESS_GEMINI_APPROVAL_MODE", "plan")
    tool_timeout_sec: float = float(os.getenv("GEMNESS_TOOL_TIMEOUT_SEC", "120"))
    diff_limit_bytes: int = _int_env("GEMNESS_DIFF_LIMIT_BYTES", 160_000)
    workspace_root: Path | None = _optional_path_env("GEMNESS_WORKSPACE_ROOT")
    allowed_roots: tuple[Path, ...] = _path_list_env("GEMNESS_ALLOWED_ROOTS")

    @classmethod
    def from_env(cls) -> "GemnessConfig":
        return cls()
