from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_trust import CONFIG_MISSING, CONFIG_UNREADABLE, UNTRUSTED, codex_trust_for_path, trusted_project_roots
from .config import GemnessConfig


POLICY_EXPLICIT_ALLOWED_ROOTS = "explicit_allowed_roots"
POLICY_AUTOMATIC_CODEX_TRUST = "automatic_codex_trust"
POLICY_NO_POLICY = "no_policy"


@dataclass(frozen=True, slots=True)
class WorkspaceDecision:
    cwd: Path
    requested_cwd: str | None
    exists: bool
    is_dir: bool
    allowed: bool
    allowed_by: str | None
    policy_mode: str
    message: str | None
    allowed_roots: tuple[Path, ...]
    explicit_allowed_roots: tuple[Path, ...]
    workspace_root: Path | None
    codex_config_path: Path
    codex_trusted_roots: tuple[Path, ...]
    codex_trust_for_cwd: str
    codex_trust_level: str | None
    matched_codex_project: Path | None
    diagnostics: tuple[str, ...]
    fixes: tuple[str, ...]

    def to_workspace_payload(self) -> dict[str, Any]:
        return {
            "cwd": str(self.cwd),
            "allowed": self.allowed,
            "allowed_by": self.allowed_by,
            "policy_mode": self.policy_mode,
            "workspace_root": str(self.workspace_root) if self.workspace_root is not None else None,
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "explicit_allowed_roots": [str(root) for root in self.explicit_allowed_roots],
            "codex_config_path": str(self.codex_config_path),
            "codex_trusted_roots": [str(root) for root in self.codex_trusted_roots],
            "codex_trust_for_cwd": self.codex_trust_for_cwd,
            "codex_trust_level": self.codex_trust_level,
            "matched_codex_project": str(self.matched_codex_project) if self.matched_codex_project is not None else None,
            "diagnostics": list(self.diagnostics),
            "warnings": list(self.diagnostics),
            "error": self.message if not self.allowed else None,
            "fixes": list(self.fixes),
        }

    def to_error_payload(self) -> dict[str, Any]:
        return {
            "status": "error",
            "message": self.message or "Workspace cwd is not allowed.",
            **self.to_workspace_payload(),
        }


class WorkspaceAccessError(ValueError):
    def __init__(self, decision: WorkspaceDecision) -> None:
        super().__init__(decision.message or "Workspace cwd is not allowed.")
        self.decision = decision

    def to_payload(self) -> dict[str, Any]:
        return self.decision.to_error_payload()


def resolve_workspace_cwd(config: GemnessConfig, requested_cwd: str | None = None) -> Path:
    decision = inspect_workspace_policy(config, requested_cwd)
    if not decision.allowed:
        raise WorkspaceAccessError(decision)
    return decision.cwd


def inspect_workspace_policy(config: GemnessConfig, requested_cwd: str | None = None) -> WorkspaceDecision:
    candidate = _candidate_cwd(config, requested_cwd)
    exists = candidate.exists()
    is_dir = candidate.is_dir()
    explicit_roots = _normalized_roots(config.allowed_roots)
    workspace_root = _normalized_optional_root(config.workspace_root)
    codex_decision = codex_trust_for_path(candidate)
    codex_trusted_roots = codex_decision.trusted_roots
    automatic_roots = _dedupe_paths(tuple(root for root in (workspace_root, *codex_trusted_roots) if root is not None))
    allowed_roots = explicit_roots or automatic_roots
    diagnostics = list(codex_decision.diagnostics)

    if explicit_roots:
        policy_mode = POLICY_EXPLICIT_ALLOWED_ROOTS
        allowed_by = "explicit_allowed_roots" if _under_any(candidate, explicit_roots) else None
        message = None if allowed_by else f"cwd outside allowed roots (explicit): {candidate}"
    else:
        policy_mode = POLICY_AUTOMATIC_CODEX_TRUST if automatic_roots else POLICY_NO_POLICY
        allowed_by = _automatic_allowed_by(candidate, workspace_root, codex_decision.status)
        message = _automatic_denial_message(candidate, allowed_by, policy_mode, codex_decision.status, codex_decision.matched_project_path)

    if not exists or not is_dir:
        allowed_by = None
        message = f"Invalid cwd: {candidate}"

    if allowed_by is None and config.allow_untrusted_cwd_fallback and exists and is_dir:
        allowed_by = "untrusted_cwd_fallback"
        message = None
        diagnostics.append("GEMNESS_ALLOW_UNTRUSTED_CWD_FALLBACK is enabled; cwd is allowed without a workspace trust policy.")

    allowed = allowed_by is not None
    return WorkspaceDecision(
        cwd=candidate,
        requested_cwd=requested_cwd,
        exists=exists,
        is_dir=is_dir,
        allowed=allowed,
        allowed_by=allowed_by,
        policy_mode=policy_mode,
        message=message,
        allowed_roots=allowed_roots,
        explicit_allowed_roots=explicit_roots,
        workspace_root=workspace_root,
        codex_config_path=codex_decision.codex_config_path,
        codex_trusted_roots=codex_trusted_roots,
        codex_trust_for_cwd=codex_decision.status,
        codex_trust_level=codex_decision.matched_trust_level,
        matched_codex_project=codex_decision.matched_project_path,
        diagnostics=tuple(diagnostics),
        fixes=_fixes(policy_mode, explicit_roots),
    )


def normalized_allowed_roots(config: GemnessConfig) -> tuple[Path, ...]:
    explicit_roots = _normalized_roots(config.allowed_roots)
    if explicit_roots:
        return explicit_roots
    workspace_root = _normalized_optional_root(config.workspace_root)
    roots = tuple(root for root in (workspace_root, *trusted_project_roots()) if root is not None)
    return _dedupe_paths(roots)


def _candidate_cwd(config: GemnessConfig, requested_cwd: str | None) -> Path:
    raw = requested_cwd or config.workspace_root or Path.cwd()
    candidate = Path(raw).expanduser()
    if requested_cwd and not candidate.is_absolute() and config.workspace_root is not None:
        candidate = Path(config.workspace_root).expanduser() / candidate
    return candidate.resolve(strict=False)


def _automatic_allowed_by(candidate: Path, workspace_root: Path | None, codex_status: str) -> str | None:
    if workspace_root is not None and _contains(workspace_root, candidate):
        return "workspace_root"
    if codex_status == "trusted":
        return "codex_trusted_project"
    return None


def _automatic_denial_message(
    candidate: Path,
    allowed_by: str | None,
    policy_mode: str,
    codex_status: str,
    matched_codex_project: Path | None,
) -> str | None:
    if allowed_by is not None:
        return None
    if codex_status == UNTRUSTED and matched_codex_project is not None:
        return f"cwd denied by closest Codex project trust_level=untrusted: {matched_codex_project}"
    if codex_status == CONFIG_UNREADABLE:
        return f"cwd is not allowed because the Codex config could not be read: {candidate}"
    if codex_status == CONFIG_MISSING and policy_mode == POLICY_NO_POLICY:
        return f"cwd is not allowed because no Gemness workspace policy or Codex trusted project is configured: {candidate}"
    if policy_mode == POLICY_NO_POLICY:
        return f"cwd is not allowed because no Gemness workspace policy trusts it: {candidate}"
    return f"cwd is not under a Gemness workspace root or Codex trusted project: {candidate}"


def _fixes(policy_mode: str, explicit_roots: tuple[Path, ...]) -> tuple[str, ...]:
    if policy_mode == POLICY_EXPLICIT_ALLOWED_ROOTS or explicit_roots:
        return (
            "For strict mode, set GEMNESS_ALLOWED_ROOTS to include this path.",
            "Or remove GEMNESS_ALLOWED_ROOTS to use Codex trusted projects automatic mode.",
        )
    return (
        "Trust this project in Codex, then restart Codex.",
        "Run: gemness bootstrap-codex",
        "For strict mode, set GEMNESS_ALLOWED_ROOTS to include this path.",
    )


def _normalized_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    return _dedupe_paths(tuple(Path(root).expanduser().resolve(strict=False) for root in roots))


def _normalized_optional_root(root: Path | None) -> Path | None:
    return Path(root).expanduser().resolve(strict=False) if root is not None else None


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return tuple(deduped)


def _under_any(candidate: Path, roots: tuple[Path, ...]) -> bool:
    return any(_contains(root, candidate) for root in roots)


def _contains(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents
