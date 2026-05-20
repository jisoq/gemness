from __future__ import annotations

import os
from pathlib import Path

from .codex_install import upsert_marked_block


START_MARKER = "<!-- gemness-trigger:start -->"
END_MARKER = "<!-- gemness-trigger:end -->"

TRIGGER_BLOCK = f"""{START_MARKER}

## Gemness / Gemness trigger

When the user says **"use gemness"** or clearly asks to use Gemness, treat it as an explicit request to consult Antigravity CLI (`agy`) through the local Gemness MCP server.

Expected behavior:

1. Prefer the MCP tools exposed by the `gemness` server.
2. If `antigravity_health` is available, call it first when connection status is uncertain.
3. Choose the tool based on the task:
   - Use `review_current_diff_with_antigravity` for current workspace change review.
   - Use `ask_antigravity_json` when a structured JSON result is needed.
   - Use `ask_antigravity` for general second opinion, architecture critique, debugging advice, or cross-checking.
   - Use `follow_up_antigravity` with the previous `session_id` when continuing the same Gemness observer conversation.
4. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
5. Treat Antigravity output as advisory, not authoritative.
6. Verify Antigravity's suggestions before applying them.
7. Summarize what Antigravity said and what was accepted, rejected, or left unverified.
8. If the MCP server or tool is unavailable, say that Gemness is not connected and provide the next setup step instead of silently skipping Antigravity.
9. Do not send secrets, private keys, credentials, or raw `.env` values to Antigravity.

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
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `antigravity_health` exists, call it first.
2. Select the right tool:
   - `review_current_diff_with_antigravity` for current workspace change review.
   - `ask_antigravity_json` for schema-constrained structured output.
   - `ask_antigravity` for general second opinion or reasoning review.
   - `follow_up_antigravity` for continuing the same Gemness observer conversation.
3. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
4. Do not include secrets or credentials.
5. Treat Antigravity's result as advisory.
6. Verify before applying changes.
7. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

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
