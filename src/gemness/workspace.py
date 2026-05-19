from __future__ import annotations

from pathlib import Path

from .config import GemnessConfig


def resolve_workspace_cwd(config: GemnessConfig, requested_cwd: str | None = None) -> Path:
    raw = requested_cwd or config.workspace_root or Path.cwd()
    candidate = Path(raw).expanduser()
    if requested_cwd and not candidate.is_absolute() and config.workspace_root is not None:
        candidate = Path(config.workspace_root) / candidate
    candidate = candidate.resolve()

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Invalid cwd: {candidate}")

    allowed_roots = _effective_allowed_roots(config)
    if allowed_roots and not any(candidate == root or root in candidate.parents for root in allowed_roots):
        raise ValueError(f"cwd outside allowed roots: {candidate}")

    return candidate


def normalized_allowed_roots(config: GemnessConfig) -> tuple[Path, ...]:
    return _effective_allowed_roots(config)


def _effective_allowed_roots(config: GemnessConfig) -> tuple[Path, ...]:
    roots = config.allowed_roots
    if not roots and config.workspace_root is not None:
        roots = (config.workspace_root,)
    return tuple(Path(root).expanduser().resolve() for root in roots)
