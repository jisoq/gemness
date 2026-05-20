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

<!-- gemness-trigger:end -->
