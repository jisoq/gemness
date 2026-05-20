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

1. The Codex main agent should delegate the Antigravity work to an `antigravity reviewer` subagent instead of calling Gemness directly in the main context. Prefer a lightweight high-reasoning reviewer profile such as `gpt-5.5-mini` with `xhigh` reasoning when available.
2. The reviewer subagent calls the blocking final-result Gemness tools and waits there until Antigravity finishes.
3. If connection status is uncertain and `antigravity_health` is available, the reviewer subagent may call it first.
4. The reviewer subagent chooses the final-result tool based on the task:
   - Use `review_current_diff_with_antigravity` for current workspace change review.
   - Use `ask_antigravity_json` when a structured JSON result is needed.
   - Use `ask_antigravity` for general second opinion, architecture critique, debugging advice, or cross-checking.
   - Use `follow_up_antigravity` with the previous `session_id` when continuing the same Gemness observer conversation.
5. The subagent must return a concise final advisory summary to the parent. It must not return only `run_id`, `observer_url`, or `accepted`.
6. The main agent verifies the advisory result before applying changes and reports what was accepted, rejected, or left uncertain.
7. `start_antigravity`, `get_antigravity_run`, `await_antigravity_run`, and `cancel_antigravity_run` are advanced background/batch APIs. Use them only when the user explicitly asks for detached/background execution.
8. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
9. If the MCP server or tool is unavailable, say that Gemness is not connected and provide the next setup step instead of silently skipping Antigravity.
10. Do not send secrets, private keys, credentials, or raw `.env` values to Antigravity.

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

1. Spawn or delegate to an `antigravity reviewer` subagent when the environment supports subagents. Prefer a lightweight high-reasoning reviewer profile such as `gpt-5.5-mini` with `xhigh` reasoning when available. Keep the main Codex context focused on orchestration and verification.
2. The reviewer subagent should call Gemness, wait for completion, and return a concise final advisory. It must not return only `run_id`, `observer_url`, or an accepted/running status.
3. If connection status is uncertain and `antigravity_health` exists, the reviewer may call it first.
4. Select the final-result tool:
   - `review_current_diff_with_antigravity` for current workspace change review.
   - `ask_antigravity_json` for schema-constrained structured output.
   - `ask_antigravity` for general second opinion or reasoning review.
   - `follow_up_antigravity` for continuing the same Gemness observer conversation.
5. Treat `start_*`, `get_antigravity_run`, `await_antigravity_run`, and `cancel_antigravity_run` as advanced detached/background APIs. Use them only when the user explicitly asks for that mode.
6. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
7. Do not include secrets or credentials.
8. Treat Antigravity's result as advisory.
9. Verify before applying changes.
10. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

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
