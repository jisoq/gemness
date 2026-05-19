---
name: gemness
description: use gemness, gemness, Gemness로 확인, Gemini second opinion 요청 시 local gemness MCP server를 사용해 Gemini CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Gemini CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `health_check` exists, call it first.
2. Select the right tool:
   - `review_current_diff` for git diff review.
   - `ask_json` for schema-constrained structured output.
   - `ask_text` for general second opinion or reasoning review.
3. Include only the necessary context.
4. Do not include secrets or credentials.
5. Treat Gemini's result as advisory.
6. Verify before applying changes.
7. Report back with what Gemness/Gemini said, what was accepted, what was rejected, and what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
