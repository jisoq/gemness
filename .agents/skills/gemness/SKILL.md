---
name: gemness
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

1. Spawn or delegate to an `antigravity reviewer` subagent when the environment supports subagents. Prefer a lightweight high-reasoning reviewer profile such as `gpt-5.5-mini` with `xhigh` reasoning when available. Keep the main Codex context focused on orchestration and verification.
2. The reviewer subagent should start Gemness work with `start_antigravity`, wait or poll with `await_antigravity_run` until completion, and return a concise final advisory. It must not return only `run_id`, `observer_url`, or an accepted/running status. It should not paste the full Antigravity answer back to the parent unless the parent explicitly asks for it; keep the parent report focused on conclusions, accepted/rejected advice, and residual uncertainty.
3. Before any Gemness tool call, determine the current workspace cwd as an absolute path:
   - Prefer `git rev-parse --show-toplevel` when the current directory is inside a git repository.
   - If that fails, use the current working directory's absolute path.
   - Pass this cwd to `antigravity_health`, `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, and `start_antigravity`.
   - Do not omit cwd and fall back to the MCP server process start directory.
   - `follow_up_antigravity` has no cwd argument; it should continue from the parent session's stored `project_root`.
4. If connection status is uncertain and `antigravity_health` exists, the reviewer may call it first with cwd.
5. Select the `start_antigravity` mode:
   - `mode="review_current_diff"` for current workspace change review.
   - `mode="json"` for schema-constrained structured output.
   - `mode="ask"` for general second opinion or reasoning review.
   - `mode="follow_up"` for continuing the same Gemness observer conversation.
6. Use `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, or `follow_up_antigravity` only as blocking convenience wrappers when a simpler one-shot call is more appropriate than explicit start/poll handling.
7. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
8. Do not include secrets or credentials.
9. Treat Antigravity's result as advisory.
10. Verify before applying changes.
11. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

## Token observability guidance

- Gemness preserves full run results. Completed `await_antigravity_run` payloads include the full result surface plus `summary`, `budget`, `observer_url`, `session_id`, and `run_id`.
- Treat `budget` as approximate telemetry for spotting duplicate or wasteful multi-LLM usage, not as exact billing data.
- `request_fingerprint`, `workspace_fingerprint`, and `workspace_fingerprint_degraded` are recording signals for future dedupe/compaction decisions. Automatic dedupe is off by default (`GEMNESS_ENABLE_AUTO_DEDUPE=false`), and matching fingerprints do not currently imply run reuse.
- Do not paste diffs, raw logs, full transcripts, or full Antigravity answers into the parent conversation when a concise advisory is enough.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running `antigravity_health` or checking Codex MCP configuration.
