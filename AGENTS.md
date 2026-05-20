# Repository Agent Instructions

## Language

- Always answer the user in Korean unless the user explicitly requests another language.

## Observer Server Process Management

- The Observer browser port is fixed at `56755`. Always use `http://127.0.0.1:56755` for browser checks.

## Working Tree Safety

- Do not revert or overwrite user changes unless the user explicitly requests it.
- Keep changes narrowly scoped to the user's approved request.
- Prefer `rg` for searching files and text.

<!-- gemness-trigger:start -->

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

<!-- gemness-trigger:end -->
