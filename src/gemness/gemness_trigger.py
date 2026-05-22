from __future__ import annotations

import os
from pathlib import Path


START_MARKER = "<!-- gemness-trigger:start -->"
END_MARKER = "<!-- gemness-trigger:end -->"

SKILL_CONTENT = """---
name: gemness
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

1. Treat the `antigravity reviewer` subagent flow as the first-priority Gemness path, not a mere recommendation. A user request that triggers this skill is a request to use Gemness through that reviewer flow unless the host proves it cannot spawn/delegate.
2. On the first Gemness health check in a Codex host, discover whether subagent, spawn, or delegation tooling is available. If the host exposes those tools lazily, use host tool discovery first. Then call `antigravity_health` with `codex_multi_agent_available=true` or `false` and a short `codex_multi_agent_evidence` value so Gemness can store the result in the user-level host capability cache.
3. On later Gemness tasks, read the `codex_host` capability returned by `antigravity_health`, or the persisted `~/.gemness/codex-host-capabilities.json` cache when already known. If `codex_host.multi_agent.available` is true, spawn or delegate to the reviewer subagent first without re-probing every repository. Re-probe only when the cached state is missing, unreadable, unknown, or the spawn call itself fails.
4. If multi-agent support is unavailable, unknown after probing, or blocked by the host, state that fallback briefly before using Gemness MCP tools directly from the main agent. Do not silently use blocking main-agent calls when the cached host capability says multi-agent is available.
5. The reviewer subagent should start Gemness work with `start_antigravity`, wait or poll with `await_antigravity_run` until completion, and return a concise final advisory. It must not return only `run_id`, `observer_url`, or an accepted/running status. It should not paste the full Antigravity answer back to the parent unless the parent explicitly asks for it; keep the parent report focused on conclusions, accepted/rejected advice, and residual uncertainty.
6. Before any Gemness tool call, determine the current workspace cwd as an absolute path:
   - Prefer `git rev-parse --show-toplevel` when the current directory is inside a git repository.
   - If that fails, use the current working directory's absolute path.
   - Pass this cwd to `antigravity_health`, `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, and `start_antigravity`.
   - Do not omit cwd and fall back to the MCP server process start directory.
   - `follow_up_antigravity` has no cwd argument; it should continue from the parent session's stored `project_root`.
7. If connection status is uncertain and `antigravity_health` exists, the reviewer may call it first with cwd.
8. Select the `start_antigravity` mode:
   - `mode="review_current_diff"` for current workspace change review.
   - `mode="json"` for schema-constrained structured output.
   - `mode="ask"` for general second opinion or reasoning review.
   - `mode="follow_up"` for continuing the same Gemness observer conversation.
9. Use `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, or `follow_up_antigravity` only as blocking convenience wrappers when the multi-agent reviewer flow is unavailable or a simpler one-shot call is explicitly more appropriate than explicit start/poll handling.
10. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
11. Do not include secrets or credentials.
12. Treat Antigravity's result as advisory.
13. Verify before applying changes.
14. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

## Token observability guidance

- Gemness preserves full run results. Completed `await_antigravity_run` payloads include the full result surface plus `summary`, `budget`, `observer_url`, `session_id`, and `run_id`.
- Treat `budget` as approximate telemetry for spotting duplicate or wasteful multi-LLM usage, not as exact billing data.
- `request_fingerprint`, `workspace_fingerprint`, and `workspace_fingerprint_degraded` are recording signals for future dedupe/compaction decisions. Automatic dedupe is off by default (`GEMNESS_ENABLE_AUTO_DEDUPE=false`), and matching fingerprints do not currently imply run reuse.
- Do not paste diffs, raw logs, full transcripts, or full Antigravity answers into the parent conversation when a concise advisory is enough.

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
    updated: list[Path] = []
    if _remove_agents_trigger_block(agents_path):
        updated.append(agents_path)
    _write_skill(skill_path)
    updated.append(skill_path)
    return updated


def install_user() -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    agents_path = codex_home / "AGENTS.md"
    skill_path = Path.home() / ".agents" / "skills" / "gemness" / "SKILL.md"
    updated: list[Path] = []
    if _remove_agents_trigger_block(agents_path):
        updated.append(agents_path)
    _write_skill(skill_path)
    updated.append(skill_path)
    return updated


def _remove_agents_trigger_block(path: Path) -> bool:
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    updated = remove_trigger_block(existing)
    if updated == existing:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def remove_trigger_block(existing: str) -> str:
    if START_MARKER not in existing and END_MARKER not in existing:
        return existing
    if START_MARKER in existing and END_MARKER in existing:
        start = existing.index(START_MARKER)
        end = existing.index(END_MARKER, start) + len(END_MARKER)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].lstrip()
        if prefix and suffix:
            return prefix + "\n\n" + suffix.rstrip() + "\n"
        if prefix:
            return prefix + "\n"
        if suffix:
            return suffix.rstrip() + "\n"
        return ""
    if START_MARKER in existing:
        return existing[: existing.index(START_MARKER)].rstrip() + "\n"
    end = existing.index(END_MARKER) + len(END_MARKER)
    return existing[end:].lstrip()


def _write_skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_CONTENT.rstrip() + "\n", encoding="utf-8")
