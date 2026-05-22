from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def codex_host_capabilities(
    path: Path,
    *,
    multi_agent_available: bool | None = None,
    evidence: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    resolved_path = path.expanduser().resolve()
    if multi_agent_available is not None:
        payload = _record_payload(resolved_path, multi_agent_available, evidence)
        try:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as exc:
            warnings.append(f"Codex host capability cache is not writable: {resolved_path} ({exc})")
        return payload, warnings

    if not resolved_path.exists():
        warnings.append("Codex multi-agent capability has not been recorded yet.")
        return _unknown_payload(resolved_path, "not_recorded"), warnings

    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Codex host capability cache is unreadable: {resolved_path} ({exc})")
        return _unknown_payload(resolved_path, "unreadable"), warnings

    return _normalize_payload(raw, resolved_path), warnings


def _record_payload(path: Path, multi_agent_available: bool, evidence: str | None) -> dict[str, Any]:
    status = "available" if multi_agent_available else "unavailable"
    warning = None if multi_agent_available else "Gemness reviewer subagent flow is unavailable in this Codex host."
    return {
        "schema_version": 1,
        "host": "codex",
        "cache_path": str(path),
        "updated_at": _now_iso(),
        "multi_agent": {
            "status": status,
            "available": multi_agent_available,
            "priority": "first",
            "source": "gemness_health_host_probe",
            "evidence": (evidence or "").strip(),
            "warning": warning,
        },
    }


def _unknown_payload(path: Path, status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host": "codex",
        "cache_path": str(path),
        "updated_at": None,
        "multi_agent": {
            "status": status,
            "available": None,
            "priority": "first",
            "source": "cache",
            "evidence": "",
            "warning": "Run Gemness health check from the main Codex agent so it can discover spawn/delegation tooling and record this host capability.",
        },
    }


def _normalize_payload(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _unknown_payload(path, "invalid")
    multi_agent = raw.get("multi_agent")
    if not isinstance(multi_agent, dict):
        return _unknown_payload(path, "invalid")
    available = multi_agent.get("available")
    if available is True:
        status = "available"
    elif available is False:
        status = "unavailable"
    else:
        status = str(multi_agent.get("status") or "unknown")
        available = None
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "host": str(raw.get("host") or "codex"),
        "cache_path": str(path),
        "updated_at": raw.get("updated_at"),
        "multi_agent": {
            "status": status,
            "available": available,
            "priority": str(multi_agent.get("priority") or "first"),
            "source": str(multi_agent.get("source") or "cache"),
            "evidence": str(multi_agent.get("evidence") or ""),
            "warning": multi_agent.get("warning"),
        },
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
