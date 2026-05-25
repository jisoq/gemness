from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OBSERVER_PORT = 56755
DEFAULT_TRANSCRIPT_DIR = Path.home() / ".gemness" / "transcripts"
DEFAULT_PROCESS_REGISTRY_DIR = Path.home() / ".gemness" / "processes"
DEFAULT_CODEX_HOST_CAPABILITIES_FILE = Path.home() / ".gemness" / "codex-host-capabilities.json"
DEFAULT_MODEL_LABEL = "Antigravity CLI default"
DEFAULT_AGY_COMMAND = "agy"
LEGACY_AGY_CAPTURE_MODES = {"auto", "pipe", "winpty"}


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _agy_capture_mode_env() -> str:
    value = os.getenv("GEMNESS_AGY_CAPTURE_MODE", "winpty").strip().lower()
    if value in LEGACY_AGY_CAPTURE_MODES:
        return "winpty"
    raise ValueError("GEMNESS_AGY_CAPTURE_MODE must be winpty")


def _optional_str_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


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
    observer_enabled: bool = field(default_factory=lambda: _bool_env("GEMNESS_OBSERVER_ENABLED", True))
    observer_host: str = field(default_factory=lambda: _loopback_host(os.getenv("GEMNESS_OBSERVER_HOST", "127.0.0.1")))
    observer_port: int = field(default_factory=lambda: _int_env("GEMNESS_OBSERVER_PORT", DEFAULT_OBSERVER_PORT))
    observer_start_on_init: bool = field(default_factory=lambda: _bool_env("GEMNESS_OBSERVER_START_ON_INIT", True))
    transcript_dir: Path = field(default_factory=lambda: Path(os.getenv("GEMNESS_TRANSCRIPT_DIR", str(DEFAULT_TRANSCRIPT_DIR))).expanduser())
    process_registry_dir: Path = field(
        default_factory=lambda: Path(os.getenv("GEMNESS_PROCESS_REGISTRY_DIR", str(DEFAULT_PROCESS_REGISTRY_DIR))).expanduser()
    )
    redact_raw_by_default: bool = field(default_factory=lambda: _bool_env("GEMNESS_REDACT_RAW_BY_DEFAULT", True))
    agy_command: str = field(default_factory=lambda: os.getenv("GEMNESS_AGY_COMMAND", DEFAULT_AGY_COMMAND))
    agy_timeout_sec: float = field(default_factory=lambda: _float_env("GEMNESS_AGY_TIMEOUT", 600.0))
    agy_health_timeout_sec: float = field(default_factory=lambda: _float_env("GEMNESS_AGY_HEALTH_TIMEOUT", 20.0))
    agy_capture_mode: str = field(default_factory=_agy_capture_mode_env)
    agy_heartbeat_interval_sec: float = field(default_factory=lambda: _float_env("GEMNESS_AGY_HEARTBEAT_INTERVAL", 5.0))
    agy_concurrency_limit: int = field(default_factory=lambda: _int_env("GEMNESS_AGY_CONCURRENCY_LIMIT", 4))
    agy_queue_limit: int = field(default_factory=lambda: _int_env("GEMNESS_AGY_QUEUE_LIMIT", 64))
    enable_auto_dedupe: bool = field(default_factory=lambda: _bool_env("GEMNESS_ENABLE_AUTO_DEDUPE", False))
    workspace_root: Path | None = field(default_factory=lambda: _optional_path_env("GEMNESS_WORKSPACE_ROOT"))
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: _path_list_env("GEMNESS_ALLOWED_ROOTS"))
    allow_untrusted_cwd_fallback: bool = field(default_factory=lambda: _bool_env("GEMNESS_ALLOW_UNTRUSTED_CWD_FALLBACK", False))
    codex_host_capabilities_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("GEMNESS_CODEX_HOST_CAPABILITIES_FILE", str(DEFAULT_CODEX_HOST_CAPABILITIES_FILE))
        ).expanduser()
    )

    @classmethod
    def from_env(cls) -> "GemnessConfig":
        return cls()
