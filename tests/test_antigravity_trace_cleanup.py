from __future__ import annotations

import re
from pathlib import Path


IGNORED_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".codex",
    ".gemness",
    ".antigravitycli",
    "build",
    "dist",
    "__pycache__",
}


def test_repository_has_no_legacy_cli_backend_traces() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = [
        "Gem" + "ini CLI",
        "gem" + "ini cli",
        "ask_" + "gem" + "ini",
        "GEMNESS_" + "GEM" + "INI",
        "gem" + "ini.started",
        "--output-" + "format",
        "stream-" + "json",
        "--session-" + "id",
        "--res" + "ume",
        "--approval-" + "mode",
        "--skip-" + "trust",
        "--reasoning-" + "effort",
    ]
    pattern = re.compile("|".join(re.escape(item) for item in forbidden), re.IGNORECASE)
    matches: list[str] = []
    for path in _repo_text_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append(f"{path.relative_to(root)}:{line_no}:{line}")
    assert matches == []


def test_remaining_brand_strings_are_allowed_antigravity_paths_or_model_names() -> None:
    root = Path(__file__).resolve().parents[1]
    needle = "gem" + "ini"
    matches: list[str] = []
    for path in _repo_text_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle.lower() not in line.lower():
                continue
            if ("~/.gem" + "ini/antigravity-cli") in line:
                continue
            if ("Gem" + "ini 3.5 Flash") in line:
                continue
            matches.append(f"{path.relative_to(root)}:{line_no}:{line}")
    assert matches == []


def _repo_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        if any(part.endswith(".egg-info") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl"}:
            continue
        files.append(path)
    return files
