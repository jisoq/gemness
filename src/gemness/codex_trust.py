from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRUSTED = "trusted"
UNTRUSTED = "untrusted"
ABSENT = "absent"
CONFIG_MISSING = "config_missing"
CONFIG_UNREADABLE = "config_unreadable"
SUPPORTED_TRUST_LEVELS = {TRUSTED, UNTRUSTED}


@dataclass(frozen=True, slots=True)
class CodexProjectTrust:
    raw_path: str
    path: Path
    trust_level: str | None


@dataclass(frozen=True, slots=True)
class CodexTrustDecision:
    candidate: Path
    status: str
    codex_config_path: Path
    matched_project: CodexProjectTrust | None
    trusted_roots: tuple[Path, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def matched_project_path(self) -> Path | None:
        return self.matched_project.path if self.matched_project is not None else None

    @property
    def matched_trust_level(self) -> str | None:
        return self.matched_project.trust_level if self.matched_project is not None else None


@dataclass(frozen=True, slots=True)
class _CodexProjectsLoad:
    config_path: Path
    status: str
    projects: tuple[CodexProjectTrust, ...]
    diagnostics: tuple[str, ...]


def get_codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home and codex_home.strip():
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def load_codex_projects() -> list[CodexProjectTrust]:
    return list(_load_codex_projects().projects)


def codex_trust_for_path(candidate: Path) -> CodexTrustDecision:
    resolved_candidate = candidate.expanduser().resolve(strict=False)
    loaded = _load_codex_projects()
    trusted_roots = tuple(project.path for project in loaded.projects if project.trust_level == TRUSTED)
    if loaded.status != "ok":
        return CodexTrustDecision(
            candidate=resolved_candidate,
            status=loaded.status,
            codex_config_path=loaded.config_path,
            matched_project=None,
            trusted_roots=trusted_roots,
            diagnostics=loaded.diagnostics,
        )

    matches = [project for project in loaded.projects if _contains(project.path, resolved_candidate)]
    if not matches:
        return CodexTrustDecision(
            candidate=resolved_candidate,
            status=ABSENT,
            codex_config_path=loaded.config_path,
            matched_project=None,
            trusted_roots=trusted_roots,
            diagnostics=loaded.diagnostics,
        )

    closest = max(matches, key=lambda project: len(project.path.parts))
    if closest.trust_level == TRUSTED:
        status = TRUSTED
    elif closest.trust_level == UNTRUSTED:
        status = UNTRUSTED
    else:
        status = ABSENT
    return CodexTrustDecision(
        candidate=resolved_candidate,
        status=status,
        codex_config_path=loaded.config_path,
        matched_project=closest,
        trusted_roots=trusted_roots,
        diagnostics=loaded.diagnostics,
    )


def trusted_project_roots() -> tuple[Path, ...]:
    return tuple(project.path for project in _load_codex_projects().projects if project.trust_level == TRUSTED)


def _load_codex_projects() -> _CodexProjectsLoad:
    config_path = get_codex_config_path().expanduser().resolve(strict=False)
    if not config_path.exists():
        return _CodexProjectsLoad(config_path, CONFIG_MISSING, (), ())
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _CodexProjectsLoad(config_path, CONFIG_UNREADABLE, (), (f"Codex config is not readable: {exc}",))

    projects_table = data.get("projects")
    if not isinstance(projects_table, dict):
        return _CodexProjectsLoad(config_path, "ok", (), ())

    projects: list[CodexProjectTrust] = []
    diagnostics: list[str] = []
    for raw_path, raw_project in projects_table.items():
        if not isinstance(raw_path, str):
            diagnostics.append(f"Codex project key is not a string: {raw_path!r}")
            continue
        trust_level = _trust_level(raw_project)
        if trust_level not in SUPPORTED_TRUST_LEVELS and trust_level is not None:
            diagnostics.append(f"Codex project {raw_path!r} has unsupported trust_level: {trust_level!r}")
        projects.append(
            CodexProjectTrust(
                raw_path=raw_path,
                path=Path(raw_path).expanduser().resolve(strict=False),
                trust_level=trust_level,
            )
        )
    return _CodexProjectsLoad(config_path, "ok", tuple(projects), tuple(diagnostics))


def _trust_level(raw_project: Any) -> str | None:
    if not isinstance(raw_project, dict):
        return None
    raw_value = raw_project.get("trust_level")
    if raw_value is None:
        return None
    return str(raw_value).strip().lower()


def _contains(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents
