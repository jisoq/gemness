# Repository Agent Instructions

## Language

- Always answer the user in Korean unless the user explicitly requests another language.

## Observer Server PID

- Use this URL for browser checks: `http://127.0.0.1:56755`.
- Do not start a new Observer server when an existing one is still running.
- Reuse the fixed PID file `.codex/observer-server.pid` for Observer UI browser checks and real Gemini Observer sessions.
- Before starting an Observer server, read `.codex/observer-server.pid` and verify whether that process is alive.
- If the process is alive, reuse its existing URL from `.codex/observer-server.url` instead of creating a new server.
- If code changes require a restart, stop only the process recorded in `.codex/observer-server.pid`, then overwrite the same PID and URL files with the restarted server.
- Do not leave ad hoc Observer server PID files behind. Clean up temporary scripts, stale PID files, and live Observer processes when the check is finished unless the user explicitly asks to keep the server running.

## Working Tree Safety

- Do not revert or overwrite user changes unless the user explicitly requests it.
- Keep changes narrowly scoped to the user's approved request.
- Prefer `rg` for searching files and text.

<!-- gemness-trigger:start -->

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

<!-- gemness-trigger:end -->
