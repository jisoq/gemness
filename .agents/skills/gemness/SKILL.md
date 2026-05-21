---
name: gemness
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

1. Spawn or delegate to an `antigravity reviewer` subagent when the environment supports subagents. Prefer a lightweight high-reasoning reviewer profile such as `gpt-5.5-mini` with `xhigh` reasoning when available. Keep the main Codex context focused on orchestration and verification.
2. The reviewer subagent should call Gemness, wait for completion, and return a concise final advisory. It must not return only `run_id`, `observer_url`, or an accepted/running status.
3. Before any Gemness tool call, determine the current workspace cwd as an absolute path:
   - Prefer `git rev-parse --show-toplevel` when the current directory is inside a git repository.
   - If that fails, use the current working directory's absolute path.
   - Pass this cwd to `antigravity_health`, `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, and any `start_*` tool call.
   - Do not omit cwd and fall back to the MCP server process start directory.
   - `follow_up_antigravity` has no cwd argument; it should continue from the parent session's stored `project_root`.
4. If connection status is uncertain and `antigravity_health` exists, the reviewer may call it first with cwd.
5. Select the final-result tool:
   - `review_current_diff_with_antigravity` for current workspace change review.
   - `ask_antigravity_json` for schema-constrained structured output.
   - `ask_antigravity` for general second opinion or reasoning review.
   - `follow_up_antigravity` for continuing the same Gemness observer conversation.
6. Treat `start_*`, `get_antigravity_run`, `await_antigravity_run`, and `cancel_antigravity_run` as advanced detached/background APIs. Use them only when the user explicitly asks for that mode.
7. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
8. Do not include secrets or credentials.
9. Treat Antigravity's result as advisory.
10. Verify before applying changes.
11. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running `antigravity_health` or checking Codex MCP configuration.
