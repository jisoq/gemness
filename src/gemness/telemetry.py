from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from subprocess import run as subprocess_run
from typing import Any


ESTIMATE_CHARS_DIV_4 = "chars_div_4"
ESTIMATE_CLI_STATS = "cli_stats"
RESPONSE_MODE_FULL = "full"

_PROMPT_TOKEN_KEYS = {
    "prompt_tokens",
    "prompt_token_count",
    "input_tokens",
    "input_token_count",
    "inputtokens",
    "prompt",
    "input",
}
_RESPONSE_TOKEN_KEYS = {
    "response_tokens",
    "response_token_count",
    "completion_tokens",
    "completion_token_count",
    "output_tokens",
    "output_token_count",
    "candidates_token_count",
    "response",
    "completion",
    "output",
}
_RESULT_TOKEN_KEYS = {
    "result_tokens",
    "result_token_count",
}


@dataclass(frozen=True, slots=True)
class WorkspaceFingerprint:
    value: str
    degraded: bool


@dataclass(frozen=True, slots=True)
class RequestProvenance:
    request_fingerprint: str
    workspace_fingerprint: str
    workspace_fingerprint_degraded: bool
    mode: str
    cwd: str | None
    schema_hash: str | None = None
    base_ref: str | None = None
    parent_session_id: str | None = None
    native_conversation_id_used: bool = False
    auto_dedupe_enabled: bool = False

    def result_fields(self) -> dict[str, Any]:
        return {
            "request_fingerprint": self.request_fingerprint,
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_fingerprint_degraded": self.workspace_fingerprint_degraded,
        }

    def metadata_fields(self) -> dict[str, Any]:
        return self.result_fields() | {
            "auto_dedupe_enabled": self.auto_dedupe_enabled,
        }

    def event_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = self.result_fields() | {
            "mode": self.mode,
            "cwd": self.cwd,
            "native_conversation_id_used": self.native_conversation_id_used,
            "auto_dedupe_enabled": self.auto_dedupe_enabled,
        }
        if self.schema_hash is not None:
            payload["schema_hash"] = self.schema_hash
        if self.base_ref is not None:
            payload["base_ref"] = self.base_ref
        if self.parent_session_id is not None:
            payload["parent_session_id"] = self.parent_session_id
        return payload


def build_budget(
    *,
    prompt: str,
    response: str,
    raw_stdout: str,
    result: str,
    duration_ms: int,
    envelope: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_chars = len(prompt or "")
    response_chars = len(response or "")
    result_chars = len(result or "")
    raw_stdout_bytes = len((raw_stdout or "").encode("utf-8", errors="replace"))
    token_sources = _token_sources(envelope, stats, metadata)

    prompt_tokens = _first_token_value(token_sources, _PROMPT_TOKEN_KEYS)
    response_tokens = _first_token_value(token_sources, _RESPONSE_TOKEN_KEYS)
    result_tokens = _first_token_value(token_sources, _RESULT_TOKEN_KEYS)
    if result_tokens is None:
        result_tokens = _first_token_value(token_sources, _RESPONSE_TOKEN_KEYS)
    used_cli_stats = any(value is not None for value in (prompt_tokens, response_tokens, result_tokens))

    return {
        "prompt_chars": prompt_chars,
        "prompt_est_tokens": prompt_tokens if prompt_tokens is not None else estimate_tokens(prompt_chars),
        "response_chars": response_chars,
        "response_est_tokens": response_tokens if response_tokens is not None else estimate_tokens(response_chars),
        "raw_stdout_bytes": raw_stdout_bytes,
        "result_chars": result_chars,
        "result_est_tokens": result_tokens if result_tokens is not None else estimate_tokens(result_chars),
        "duration_ms": max(0, int(duration_ms)),
        "response_mode": RESPONSE_MODE_FULL,
        "estimate_method": ESTIMATE_CLI_STATS if used_cli_stats else ESTIMATE_CHARS_DIV_4,
        "truncated": False,
    }


def combine_budgets(*budgets: dict[str, Any] | None) -> dict[str, Any] | None:
    values = [budget for budget in budgets if isinstance(budget, dict)]
    if not values:
        return None
    numeric_fields = (
        "prompt_chars",
        "prompt_est_tokens",
        "response_chars",
        "response_est_tokens",
        "raw_stdout_bytes",
        "result_chars",
        "result_est_tokens",
        "duration_ms",
    )
    combined: dict[str, Any] = {field: sum(int(budget.get(field) or 0) for budget in values) for field in numeric_fields}
    combined["response_mode"] = RESPONSE_MODE_FULL
    combined["estimate_method"] = ESTIMATE_CLI_STATS if any(budget.get("estimate_method") == ESTIMATE_CLI_STATS for budget in values) else ESTIMATE_CHARS_DIV_4
    combined["truncated"] = any(bool(budget.get("truncated")) for budget in values)
    return combined


def estimate_tokens(char_count: int) -> int:
    return int(math.ceil(max(0, int(char_count)) / 4))


def build_request_provenance(
    *,
    mode: str,
    prompt: str,
    cwd: Path | None,
    schema: dict[str, Any] | None = None,
    base_ref: str | None = None,
    parent_session_id: str | None = None,
    native_conversation_id: str | None = None,
    auto_dedupe_enabled: bool = False,
) -> RequestProvenance:
    workspace = workspace_fingerprint(cwd)
    schema_hash = stable_json_hash(schema) if schema is not None else None
    cwd_text = str(cwd.resolve()) if cwd is not None else None
    fingerprint_input: dict[str, Any] = {
        "mode": mode,
        "prompt": normalize_prompt(prompt),
        "cwd": cwd_text,
        "workspace_fingerprint": workspace.value,
    }
    if schema_hash is not None:
        fingerprint_input["schema_hash"] = schema_hash
    if base_ref is not None:
        fingerprint_input["base_ref"] = base_ref
    if parent_session_id is not None:
        fingerprint_input["parent_session_id"] = parent_session_id
    if native_conversation_id is not None:
        fingerprint_input["native_conversation_id"] = native_conversation_id

    return RequestProvenance(
        request_fingerprint=f"req:{_hash_json(fingerprint_input)}",
        workspace_fingerprint=workspace.value,
        workspace_fingerprint_degraded=workspace.degraded,
        mode=mode,
        cwd=cwd_text,
        schema_hash=schema_hash,
        base_ref=base_ref,
        parent_session_id=parent_session_id,
        native_conversation_id_used=bool(native_conversation_id),
        auto_dedupe_enabled=auto_dedupe_enabled,
    )


def workspace_fingerprint(cwd: Path | None) -> WorkspaceFingerprint:
    if cwd is None:
        return _degraded_workspace_fingerprint("cwd:none")
    try:
        resolved = cwd.expanduser().resolve()
    except OSError:
        return _degraded_workspace_fingerprint(f"cwd:{cwd}:unresolved")
    try:
        inside = _git(resolved, "rev-parse", "--is-inside-work-tree").strip().lower()
        if inside != "true":
            return _degraded_workspace_fingerprint(f"cwd:{resolved}:not-git")
        root = _git(resolved, "rev-parse", "--show-toplevel").strip()
        root_path = Path(root).expanduser().resolve()
        head = _git(resolved, "rev-parse", "HEAD").strip()
        status = _git(resolved, "status", "--porcelain=v1", "--untracked-files=all")
        diff = _git(resolved, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
        untracked = _git(root_path, "ls-files", "--others", "--exclude-standard", "-z")
        untracked_content_hash = _untracked_content_hash(root_path, untracked)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _degraded_workspace_fingerprint(f"cwd:{resolved}:git-failed")

    payload = {
        "kind": "git",
        "root": root,
        "head": head,
        "porcelain_status_hash": _hash_text(status),
        "diff_hash": _hash_text(diff),
        "untracked_content_hash": untracked_content_hash,
    }
    return WorkspaceFingerprint(f"git:{_hash_json(payload)}", degraded=False)


def normalize_prompt(prompt: str) -> str:
    text = str(prompt or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def stable_json_hash(value: Any) -> str:
    return f"sha256:{_hash_json(value)}"


def _token_sources(
    envelope: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> list[Any]:
    sources: list[Any] = []
    if isinstance(envelope, dict):
        sources.extend([envelope.get("stats"), envelope.get("usage"), envelope.get("metadata")])
    sources.extend([stats, metadata])
    return [source for source in sources if isinstance(source, dict)]


def _first_token_value(sources: list[Any], aliases: set[str]) -> int | None:
    for source in sources:
        value = _find_numeric_token(source, aliases)
        if value is not None:
            return value
    return None


def _find_numeric_token(value: Any, aliases: set[str]) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_key(str(key)) in aliases and _is_number(item):
                return int(math.ceil(float(item)))
        for item in value.values():
            nested = _find_numeric_token(item, aliases)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_numeric_token(item, aliases)
            if nested is not None:
                return nested
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _untracked_content_hash(root: Path, listing: str) -> str:
    hasher = hashlib.sha256()
    for relative in sorted(path for path in listing.split("\0") if path):
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            continue
        hasher.update(relative.replace("\\", "/").encode("utf-8", errors="replace"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(candidate.read_bytes()).hexdigest().encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess_run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
    return completed.stdout


def _degraded_workspace_fingerprint(marker: str) -> WorkspaceFingerprint:
    return WorkspaceFingerprint(f"degraded:{_hash_text(marker)}", degraded=True)


def _hash_json(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(serialized)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
