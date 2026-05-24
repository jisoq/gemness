---
name: gemness
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

First determine whether you are the main agent orchestrator or the reviewer subagent for this task.

## Main Agent Orchestrator

1. Treat the `antigravity reviewer` subagent flow as the first-priority Gemness path, not a mere recommendation. A user request that triggers this skill is a request to use Gemness through that reviewer flow unless the host proves it cannot spawn/delegate.
2. Use one Gemness health owner per task. If the main agent is about to spawn or delegate, the main agent owns health: determine the cwd, read or reuse the Codex host capability cache, and call `antigravity_health` only when readiness or cache state is unknown, stale, or diagnostically needed. Include a short `Gemness health handoff` in the reviewer prompt with cwd, health status, `codex_host.multi_agent.available`, and whether `antigravity_health` was already called.
3. On the first Gemness health check in a Codex host, the health owner discovers whether subagent, spawn, or delegation tooling is available. If the host exposes those tools lazily, use host tool discovery first. Then call `antigravity_health` with `codex_multi_agent_available=true` or `false` and a short `codex_multi_agent_evidence` value so Gemness can store the result in the user-level host capability cache.
4. On later Gemness tasks, read the `codex_host` capability returned by `antigravity_health`, or the persisted `~/.gemness/codex-host-capabilities.json` cache when already known. If `codex_host.multi_agent.available` is true, spawn or delegate to the reviewer subagent first without re-probing every repository. Re-probe only when the cached state is missing, unreadable, unknown, or the spawn call itself fails.
5. When spawning the reviewer with `multi_agent_v1.spawn_agent`, set `model="gpt-5.5"` and `reasoning_effort="medium"`. Leave other spawn profile fields unset unless the user explicitly asks for them.
6. When spawning the reviewer, include a `delegated_run handoff` with cwd, task, desired mode, any schema or parent session id, and a parent-generated `delegation_id`. Tell the reviewer to pass that exact `delegation_id` as `idempotency_key` to `start_antigravity`.
7. Once the reviewer is spawned, the reviewer owns that Gemness run. The main must not call `start_antigravity` / `await_antigravity_run` while reviewer owns the delegated run, and must not use `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, or `follow_up_antigravity` for the same task.
8. After spawning or delegating a reviewer in background/detached mode, the main agent should keep working on non-overlapping local tasks instead of idly waiting: inspect relevant code or diffs, run available checks, prepare acceptance criteria, or plan integration work. Wait for the reviewer only when its advisory is needed to decide or report.
9. The main may take over a delegated Gemness run only if reviewer spawn fails, the reviewer explicitly fails or times out at the subagent/task level, the reviewer returns a pending handoff or only a `run_id` without final advisory, or the user explicitly asks the main agent to run Gemness directly. A single `await_antigravity_run` timeout or an `accepted`/`queued`/`running` status is not reviewer failure and not evidence that Antigravity will not answer. On takeover, do not start a duplicate run; await, cancel, or follow up using the existing `run_id` or session identifiers when available, and do not invent advisory content while the run is non-terminal.
10. If multi-agent support is unavailable, unknown after probing, or blocked by the host, state that fallback briefly before using Gemness MCP tools directly from the main agent. Do not silently use blocking main-agent calls when the cached host capability says multi-agent is available.

## Reviewer Subagent

1. The reviewer must not spawn/delegate another subagent. It is the run owner for the delegated Gemness work it receives from the parent.
2. Trust the parent's `Gemness health handoff` and `delegated_run handoff` when they match the current cwd and task. Do not re-probe host multi-agent support from inside the reviewer.
3. When a reviewer subagent receives a `Gemness health handoff` for the same cwd with status `ok` or `warning`, it must skip `antigravity_health` and start Gemness work with `start_antigravity`. Recheck only when the parent explicitly asks for health, the cwd differs, the handoff is missing/failed/stale, or the first Gemness tool call fails in a way that needs health diagnostics.
4. If no handoff is present and the reviewer is the first Gemness actor, it may call `antigravity_health` once with cwd before `start_antigravity`; after that it owns the health result and should continue the task without asking the parent to recheck.
5. The reviewer subagent should start Gemness work with `start_antigravity`, passing the parent `delegation_id` as `idempotency_key` when present. Gemness scopes that key by tool/cwd/workspace fingerprint, so a duplicate key from a different cwd must start a distinct run. It should wait or poll with `await_antigravity_run` until a terminal status (`completed`, `valid`, `invalid`, `error`, or `cancelled`) and return a concise final advisory only from the terminal `result.text` or `result.data`.
6. Treat `accepted`, `queued`, `sending`, `running`, `repairing`, `result_pending=true`, and a single bounded `await_antigravity_run` timeout as pending state, not failure, silence, or permission to answer from the reviewer model's own reasoning. Keep polling while the subagent task budget permits. If the subagent must stop before terminal status, return an explicit pending handoff with `run_id`, `observer_url`, last status, and no advisory.
7. The reviewer must not call `cancel_antigravity_run` merely because the run is slow, the current await call timed out, or the answer has not arrived yet. Cancel only for an explicit user/parent cancellation request, a known wrong duplicate run, or a tool-reported terminal cancellation/error path.
8. The reviewer must not fabricate, infer, or summarize Antigravity's advisory from the prompt, local code inspection, progress/heartbeat events, partial stdout, or elapsed time. If no terminal result exists, say no Antigravity advisory is available yet.
9. The reviewer must not return only `run_id`, `observer_url`, or an accepted/running status as if the delegated task were complete; those are pending handoff fields only.
10. The reviewer should not paste the full Antigravity answer back to the parent unless the parent explicitly asks for it; keep the parent report focused on conclusions, accepted/rejected advice, and residual uncertainty.
11. When the parent `delegated_run handoff` requests `mode="follow_up"` and provides the parent session id plus follow-up instruction, the reviewer is only the delivery owner for that follow-up: it must forward the instruction to Antigravity with `follow_up_antigravity` or `start_antigravity(mode="follow_up")`, wait for the terminal result, and report Antigravity's answer. It must not answer the parent follow-up from the reviewer model's own reasoning. Outside an explicit parent follow-up handoff, the reviewer must not self-initiate another Antigravity turn; if another pass seems useful, recommend it to the parent.

## Shared Tool Rules

1. Before any Gemness tool call, determine the current workspace cwd as an absolute path:
   - Prefer `git rev-parse --show-toplevel` when the current directory is inside a git repository.
   - If that fails, use the current working directory's absolute path.
   - Pass this cwd to `antigravity_health`, `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, and `start_antigravity`.
   - Do not omit cwd and fall back to the MCP server process start directory.
   - `follow_up_antigravity` has no cwd argument; it should continue from the parent session's stored `project_root`.
2. Select the `start_antigravity` mode:
   - `mode="review_current_diff"` for current git workspace change review. If the cwd is not a git worktree, expect a `diff_unavailable_not_git_repo` error instead of falling back to another repository.
   - `mode="json"` for schema-constrained structured output.
   - `mode="ask"` for general second opinion or reasoning review.
   - `mode="follow_up"` for continuing the same Gemness observer conversation.
3. Use `ask_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`, or `follow_up_antigravity` only as blocking convenience wrappers when the multi-agent reviewer flow is unavailable, the main has an explicit takeover reason, or a simpler one-shot call is explicitly more appropriate than explicit start/poll handling.
4. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
5. Do not include secrets or credentials.
6. Treat Antigravity's result as advisory.
7. Verify before applying changes.
8. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

## Token observability guidance

- Gemness preserves full run results. Completed `await_antigravity_run` payloads include the full result surface plus `summary`, `budget`, `observer_url`, `session_id`, and `run_id`.
- Treat `budget` as approximate telemetry for spotting duplicate or wasteful multi-LLM usage, not as exact billing data.
- `request_fingerprint`, `workspace_fingerprint`, and `workspace_fingerprint_degraded` are recording signals for future dedupe/compaction decisions. Automatic dedupe is off by default (`GEMNESS_ENABLE_AUTO_DEDUPE=false`), and matching fingerprints do not currently imply run reuse.
- For `review_current_diff`, treat `review_scope` in the final JSON as part of the advisory contract: it should show the inspected cwd, workspace root, base ref, and reviewed changed files. If Gemness returns `invalid` with `review_scope_errors`, discard the advisory as out of scope.
- Do not paste diffs, raw logs, full transcripts, or full Antigravity answers into the parent conversation when a concise advisory is enough.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
