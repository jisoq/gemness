---
name: gemness
description: use gemness, gemness, Gemness로 확인, Antigravity second opinion 요청 시 local gemness MCP server를 사용해 Antigravity CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Antigravity CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `antigravity_health` exists, call it first.
2. Select the right tool:
   - `review_current_diff_with_antigravity` for current workspace change review.
   - `ask_antigravity_json` for schema-constrained structured output.
   - `ask_antigravity` for general second opinion or reasoning review.
   - `follow_up_antigravity` for continuing the same Gemness observer conversation.
3. Send concise task instructions. Do not paste diffs, file dumps, logs, terminal transcripts, or full conversation transcripts when Antigravity can inspect the workspace itself.
4. Do not include secrets or credentials.
5. Treat Antigravity's result as advisory.
6. Verify before applying changes.
7. Report back with what Gemness/Antigravity said, what was accepted, what was rejected, and what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running `antigravity_health` or checking Codex MCP configuration.
