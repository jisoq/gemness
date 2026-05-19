from __future__ import annotations

import os
from pathlib import Path

from .codex_install import upsert_marked_block


START_MARKER = "<!-- gemness-trigger:start -->"
END_MARKER = "<!-- gemness-trigger:end -->"

TRIGGER_BLOCK = f"""{START_MARKER}

## Gemness / Gemness trigger

When the user says **"use gemness"** or clearly asks to use Gemness, treat it as an explicit request to consult Gemini CLI through the local Gemness MCP server.

Expected behavior:

1. Prefer the MCP tools exposed by the `gemness` server.
2. If `health_check` is available, call it first when connection status is uncertain.
3. Choose the tool based on the task:
   - Use `review_current_diff` for current git diff review.
   - Use `ask_json` when a structured JSON result is needed.
   - Use `ask_text` for general second opinion, architecture critique, debugging advice, or cross-checking.
4. Treat Gemini output as advisory, not authoritative.
5. Verify Gemini's suggestions before applying them.
6. Summarize what Gemini said and what was accepted, rejected, or left unverified.
7. If the MCP server or tool is unavailable, say that Gemness is not connected and provide the next setup step instead of silently skipping Gemini.
8. Do not send secrets, private keys, credentials, or raw `.env` values to Gemini.

Trigger phrases include:

- `use gemness`
- `Use Gemness`
- `gemness로 확인`
- `Gemness로 리뷰`
- `use gemness to review`
- `use gemness for a second opinion`

{END_MARKER}
"""

SKILL_CONTENT = """---
name: gemness
description: use gemness, gemness, Gemness로 확인, Gemini second opinion 요청 시 local gemness MCP server를 사용해 Gemini CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Gemini CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `health_check` exists, call it first.
2. Select the right tool:
   - `review_current_diff` for git diff review.
   - `ask_json` for schema-constrained structured output.
   - `ask_text` for general second opinion or reasoning review.
3. Include only the necessary context.
4. Do not include secrets or credentials.
5. Treat Gemini's result as advisory.
6. Verify before applying changes.
7. Report back with what Gemness/Gemini said, what was accepted, what was rejected, and what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
"""


def install(scope: str, project_root: Path) -> list[Path]:
    project_root = project_root.expanduser().resolve()
    updated: list[Path] = []
    if scope in {"project", "both"}:
        updated.extend(install_project(project_root))
    if scope in {"user", "both"}:
        updated.extend(install_user())
    return updated


def install_project(project_root: Path) -> list[Path]:
    agents_path = project_root / "AGENTS.md"
    skill_path = project_root / ".agents" / "skills" / "gemness" / "SKILL.md"
    _upsert_agents_file(agents_path)
    _write_skill(skill_path)
    return [agents_path, skill_path]


def install_user() -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    agents_path = codex_home / "AGENTS.md"
    skill_path = Path.home() / ".agents" / "skills" / "gemness" / "SKILL.md"
    _upsert_agents_file(agents_path)
    _write_skill(skill_path)
    return [agents_path, skill_path]


def _upsert_agents_file(path: Path) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(upsert_marked_block(existing, TRIGGER_BLOCK, START_MARKER, END_MARKER), encoding="utf-8")


def _write_skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_CONTENT.rstrip() + "\n", encoding="utf-8")
