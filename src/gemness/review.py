from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from subprocess import SubprocessError, run as subprocess_run
from typing import Iterable

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "recommended_actions", "review_scope"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "needs_work", "unsafe"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "explanation"],
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line_hint": {"type": "string"},
                    "explanation": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "review_scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["cwd", "workspace_root", "base_ref", "reviewed_files"],
            "properties": {
                "cwd": {"type": "string"},
                "workspace_root": {"type": "string"},
                "base_ref": {"type": "string"},
                "reviewed_files": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class ReviewWorkspace:
    cwd: Path
    workspace_root: Path
    base_ref: str
    changed_files: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "cwd": str(self.cwd),
            "workspace_root": str(self.workspace_root),
            "base_ref": self.base_ref,
            "reviewed_files": list(self.changed_files),
        }


class ReviewWorkspaceError(ValueError):
    def __init__(self, message: str, *, reason: str, cwd: Path) -> None:
        super().__init__(message)
        self.reason = reason
        self.cwd = cwd

    def to_payload(self) -> dict[str, object]:
        return {
            "status": "error",
            "message": str(self),
            "reason": self.reason,
            "cwd": str(self.cwd),
        }


def inspect_review_workspace(cwd: Path, base_ref: str, *, git_timeout_sec: float | None = None) -> ReviewWorkspace:
    resolved = cwd.expanduser().resolve()
    try:
        inside = _git(resolved, "rev-parse", "--is-inside-work-tree", timeout_sec=git_timeout_sec).strip().lower()
    except ReviewWorkspaceError as exc:
        if exc.reason != "diff_unavailable_not_git_repo":
            raise
        raise ReviewWorkspaceError(
            f"Current diff unavailable: cwd is not a git repository: {resolved}",
            reason="diff_unavailable_not_git_repo",
            cwd=resolved,
        ) from exc
    if inside != "true":
        raise ReviewWorkspaceError(
            f"Current diff unavailable: cwd is not a git repository: {resolved}",
            reason="diff_unavailable_not_git_repo",
            cwd=resolved,
        )
    try:
        root = Path(_git(resolved, "rev-parse", "--show-toplevel", timeout_sec=git_timeout_sec).strip()).expanduser().resolve()
        changed_files = _changed_files(resolved, root, base_ref, timeout_sec=git_timeout_sec)
    except ReviewWorkspaceError:
        raise
    except OSError as exc:
        raise ReviewWorkspaceError(
            f"Current diff unavailable for cwd {resolved}: {exc}",
            reason="diff_unavailable_git_error",
            cwd=resolved,
        ) from exc
    return ReviewWorkspace(cwd=resolved, workspace_root=root, base_ref=base_ref, changed_files=tuple(changed_files))


def build_review_prompt(base_ref: str, review_workspace: ReviewWorkspace) -> str:
    scope_json = json.dumps(review_workspace.to_payload(), ensure_ascii=False, indent=2)
    return (
        "Review the current repository changes as an advisory reviewer. Gemness has not embedded a diff; "
        "use Antigravity CLI's own repository inspection capabilities from the current working directory "
        "to inspect changed files and compare them with the requested base reference as needed. Treat the "
        "workspace scope below as authoritative: do not reuse prior session output, do not inspect or report "
        "findings for files outside this workspace, and do not review a different repository if the current "
        "working directory does not match. Do not modify files, do not read or quote secrets/private keys/raw "
        "environment values, and return only JSON that matches the supplied schema. Focus on correctness, "
        "security, data loss, and test gaps.\n\n"
        "Workspace review scope:\n"
        f"{scope_json}\n\n"
        "In `review_scope`, return the cwd and workspace_root you actually inspected, the same base_ref, and "
        "the changed file list you reviewed using repository-relative paths with forward slashes. If a changed "
        "file could not be inspected, keep it in reviewed_files and describe the verification gap.\n\n"
        f"Base ref: {base_ref}"
    )


def validate_review_scope(data: object, review_workspace: ReviewWorkspace) -> list[dict[str, object]]:
    if not isinstance(data, dict):
        return [_scope_error("review_scope", "review response must be an object")]
    scope = data.get("review_scope")
    if not isinstance(scope, dict):
        return [_scope_error("review_scope", "review_scope is required")]

    errors: list[dict[str, object]] = []
    expected_cwd = str(review_workspace.cwd)
    expected_root = str(review_workspace.workspace_root)
    if scope.get("cwd") != expected_cwd:
        errors.append(_scope_error("review_scope.cwd", f"expected {expected_cwd!r}, got {scope.get('cwd')!r}"))
    if scope.get("workspace_root") != expected_root:
        errors.append(_scope_error("review_scope.workspace_root", f"expected {expected_root!r}, got {scope.get('workspace_root')!r}"))
    if scope.get("base_ref") != review_workspace.base_ref:
        errors.append(_scope_error("review_scope.base_ref", f"expected {review_workspace.base_ref!r}, got {scope.get('base_ref')!r}"))

    reviewed_files = scope.get("reviewed_files")
    if not isinstance(reviewed_files, list) or not all(isinstance(item, str) for item in reviewed_files):
        errors.append(_scope_error("review_scope.reviewed_files", "reviewed_files must be an array of strings"))
        return errors
    normalized_reviewed = tuple(_normalize_relative_path(item) for item in reviewed_files)
    expected_files = review_workspace.changed_files
    if tuple(sorted(normalized_reviewed)) != tuple(sorted(expected_files)):
        errors.append(
            _scope_error(
                "review_scope.reviewed_files",
                f"expected changed files {list(expected_files)!r}, got {list(normalized_reviewed)!r}",
            )
        )
    return errors


def _changed_files(cwd: Path, root: Path, base_ref: str, *, timeout_sec: float | None) -> list[str]:
    pathspec = _cwd_pathspec(cwd, root)
    if base_ref == "HEAD" and not _ref_exists(root, "HEAD", timeout_sec=timeout_sec):
        files = _git_paths(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", pathspec, timeout_sec=timeout_sec)
        return _dedupe_sorted(_normalize_relative_path(path) for path in files)
    diff_files = _git_paths(root, "diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB", base_ref, "--", pathspec, timeout_sec=timeout_sec)
    untracked = _git_paths(root, "ls-files", "-z", "--others", "--exclude-standard", "--", pathspec, timeout_sec=timeout_sec)
    return _dedupe_sorted(_normalize_relative_path(path) for path in (*diff_files, *untracked))


def _git(cwd: Path, *args: str, timeout_sec: float | None) -> str:
    completed = _run_git(cwd, *args, text=True, timeout_sec=timeout_sec)
    return str(completed.stdout)


def _git_paths(cwd: Path, *args: str, timeout_sec: float | None) -> list[str]:
    completed = _run_git(cwd, *args, text=False, timeout_sec=timeout_sec)
    stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode("utf-8", errors="replace")
    try:
        return _decode_nul_paths(stdout)
    except UnicodeDecodeError as exc:
        raise ReviewWorkspaceError(
            f"Current diff unavailable for cwd {cwd}: git reported a non-UTF-8 path",
            reason="diff_unavailable_git_error",
            cwd=cwd,
        ) from exc


def _run_git(cwd: Path, *args: str, text: bool, timeout_sec: float | None):
    try:
        completed = subprocess_run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
            timeout=timeout_sec,
        )
    except (OSError, SubprocessError) as exc:
        raise ReviewWorkspaceError(f"Current diff unavailable for cwd {cwd}: {exc}", reason="diff_unavailable_git_error", cwd=cwd) from exc
    if completed.returncode != 0:
        message = _completed_output_text(completed.stderr) or _completed_output_text(completed.stdout)
        message = message.strip() or f"git {' '.join(args)} failed with exit code {completed.returncode}"
        reason = "diff_unavailable_not_git_repo" if "rev-parse" in args else "diff_unavailable_git_error"
        raise ReviewWorkspaceError(f"Current diff unavailable for cwd {cwd}: {message}", reason=reason, cwd=cwd)
    return completed


def _cwd_pathspec(cwd: Path, root: Path) -> str:
    if cwd == root:
        return "."
    return cwd.relative_to(root).as_posix()


def _ref_exists(root: Path, ref: str, *, timeout_sec: float | None) -> bool:
    try:
        completed = subprocess_run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, SubprocessError) as exc:
        raise ReviewWorkspaceError(f"Current diff unavailable for cwd {root}: {exc}", reason="diff_unavailable_git_error", cwd=root) from exc
    return completed.returncode == 0


def _dedupe_sorted(paths: Iterable[str]) -> list[str]:
    return sorted({path for path in paths if path})


def _decode_nul_paths(stdout: bytes) -> list[str]:
    return [part.decode("utf-8") for part in stdout.split(b"\0") if part]


def _completed_output_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _normalize_relative_path(path: str) -> str:
    value = str(path).strip("/")
    parts = [part for part in value.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return value
    return "/".join(parts)


def _scope_error(path: str, message: str) -> dict[str, object]:
    return {"path": path, "message": message}
