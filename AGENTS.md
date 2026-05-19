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
