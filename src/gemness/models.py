from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ObserverRole = Literal["codex_mcp", "gemness", "user", "system"]
SessionStatus = Literal[
    "queued",
    "waiting_for_user_approval",
    "sending",
    "running",
    "repairing",
    "valid",
    "invalid",
    "error",
    "cancelled",
    "completed",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class ObserverEvent:
    event_id: str
    session_id: str
    ts: str
    type: str
    role: ObserverRole
    payload: dict[str, Any]
    parent_session_id: str | None = None
    tool_name: str | None = None
    phase: str | None = None
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    tool_name: str
    model: str
    status: SessionStatus
    started_at: str
    parent_session_id: str | None = None
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    duration_ms: int | None = None
    valid: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(slots=True)
class Intervention:
    intervention_id: str
    session_id: str
    action: str
    ts: str
    instruction: str | None = None
    prompt: str | None = None
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

