# Gemness Observer Architecture

## Overview

```mermaid
flowchart LR
  User["User"]
  Codex["Codex agent"]
  Tools["MCP tools: health_check, ask_text, ask_json, review_current_diff"]
  Hub["ObserverHub"]
  Runner["GeminiCliRunner"]
  UI["Loopback browser UI"]
  Gemini["gemini CLI"]

  User --> Codex --> Tools
  Tools --> Hub
  Tools --> Runner --> Gemini
  Runner --> Hub
  Hub --> UI
  UI --> Hub
  Tools --> Codex --> User
```

`ObserverHub` owns session IDs, session state, events, redaction, intervention queues, JSONL transcript persistence, and the local web server. Gemini remains advisory: Codex still decides how to use the returned text or JSON.

## Tool Pipeline

`ask_text`:

1. create session
2. render and redact prompt events
3. optionally wait for approval or prompt edit
4. run Gemini CLI with `--output-format json`
5. record response, stderr, exit, and final result

`ask_json`:

1. render prompt plus JSON Schema
2. run Gemini CLI
3. parse CLI JSON envelope and extract `response`
4. remove code fences and extract JSON candidate
5. parse JSON
6. validate against JSON Schema
7. if parse or validation fails, run one repair prompt
8. return `valid`, `invalid`, or `error`

`review_current_diff`:

1. MCP server resolves the configured workspace `cwd`
2. MCP server validates `base_ref`
3. MCP server runs `git diff --no-color <base_ref> --` in the resolved `cwd`
2. diff is size-limited
3. diff is included in a review prompt
4. review response is validated against the built-in review schema
5. UI renders review findings when present

`health_check`:

1. resolves the requested or configured workspace `cwd`
2. reports server, Python, observer, transcript, workspace, and Gemini CLI configuration
3. checks Gemini CLI command resolution and version without calling a model
4. returns warnings instead of crashing when Gemini CLI is missing or unavailable

## Gemini CLI Output Choice

This implementation uses `--output-format json` as the default canonical mode. The goal file notes that `stream-json` can expose realtime deltas, but `ask_json` correctness depends on a stable final response for envelope parsing, JSON extraction, validation, and repair. The runner records final response events reliably and keeps the design open for a future stream-json runner.

For Gemini CLI headless mode, the runner defaults to `--approval-mode plan` and does not pass `--skip-trust`. It also sets `GEMINI_CLI_TRUST_WORKSPACE=true` for the Gemini child process so real observer sessions do not stop on Gemini CLI's interactive workspace trust prompt. Set `GEMNESS_GEMINI_TRUST_WORKSPACE=false` or `GEMINI_CLI_TRUST_WORKSPACE=false` only when you explicitly want to disable that workspace trust environment value. Set `GEMNESS_GEMINI_SKIP_TRUST=true` only when you explicitly want to bypass Gemini CLI trust checks in your local environment.

## Event Schema

Events are persisted as JSONL under `GEMNESS_TRANSCRIPT_DIR`.

```ts
type ObserverEvent = {
  event_id: string;
  session_id: string;
  parent_session_id?: string;
  ts: string;
  type:
    | "session.created"
    | "prompt.rendered"
    | "prompt.redacted"
    | "prompt.pending_approval"
    | "prompt.sent"
    | "gemini.started"
    | "gemini.delta"
    | "gemini.response"
    | "gemini.stderr"
    | "gemini.exited"
    | "json.extracted"
    | "json.parse_failed"
    | "json.validation_failed"
    | "json.validation_passed"
    | "repair.started"
    | "repair.prompt_sent"
    | "repair.response"
    | "repair.validation_passed"
    | "repair.validation_failed"
    | "intervention.received"
    | "intervention.applied"
    | "session.completed"
    | "session.cancelled"
    | "session.error";
  role: "codex_mcp" | "gemness" | "user" | "system";
  tool_name?: "ask_text" | "ask_json" | "review_current_diff";
  phase?: string;
  payload: Record<string, unknown>;
  redacted?: boolean;
};
```

## Session State Machine

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> waiting_for_user_approval
  queued --> sending
  waiting_for_user_approval --> sending: approve
  waiting_for_user_approval --> queued: edit prompt
  waiting_for_user_approval --> cancelled: cancel
  sending --> running
  running --> repairing
  running --> completed
  running --> valid
  running --> invalid
  running --> error
  running --> cancelled
  repairing --> valid
  repairing --> invalid
  repairing --> error
  completed --> queued: follow-up child
  valid --> queued: follow-up child
  invalid --> queued: follow-up child
  error --> queued: follow-up child
```

Statuses used by sessions:

```ts
type SessionStatus =
  | "queued"
  | "waiting_for_user_approval"
  | "sending"
  | "running"
  | "repairing"
  | "valid"
  | "invalid"
  | "error"
  | "cancelled"
  | "completed";
```

## Intervention Semantics

- `edit_prompt`: allowed before send. Replaces the prompt draft and records a new `prompt.rendered` event.
- `add_instruction`: allowed before send. Appends a user intervention block to the prompt.
- `approve`: allowed while waiting for approval. Sends the current draft.
- `cancel`: allowed before send or while running. Cancels the session or terminates the subprocess.
- `interrupt_retry`: allowed while running. Terminates the subprocess and creates a child session with the original prompt, partial output, and user instruction.
- `follow_up`: allowed after completion. Creates a parent-linked child session with a redacted transcript summary.

## API

All API routes require the local token as `?token=...`, `Authorization: Bearer ...`, or `X-Observer-Token`.

- `GET /api/sessions`
- `GET /api/sessions/<session_id>?raw=0`
- `GET /api/sessions/<session_id>/export?raw=0`
- `GET /api/events?raw=0`
- `POST /api/config`
- `POST /api/sessions/<session_id>/interventions`

Intervention request body:

```json
{
  "action": "interrupt_retry",
  "instruction": "Focus on data loss risk."
}
```

## Manual Test Flow

1. Install in a project virtual environment with `python -m pip install -e .`.
2. Configure Codex with `docs/codex-mcp-config.example.toml`, including `cwd`, `enabled_tools`, and `default_tools_approval_mode = "prompt"`.
3. Run `codex mcp list`.
4. Open Codex TUI, run `/mcp`, and confirm `gemness` is active.
5. Ask Codex: `use gemness: run health check`.
6. Call `ask_text` and open the returned `observer_url`.
7. Confirm the transcript shows prompt, response, and final result.
8. Call `ask_json` with a schema.
9. Confirm JSON extraction, validation, and any repair result are visible.
10. Call `review_current_diff`.
11. Confirm review findings render in the UI.
12. Set `GEMNESS_PAUSE_BEFORE_SEND=true`, call a tool, edit the queued prompt, then approve.
13. During a long-running call, use `Interrupt and retry`.
14. On a completed session, use `Continue with instruction`.
15. Use `Export JSON` and confirm the default export is redacted.

## Known Limitations

- A headless Gemini subprocess cannot reliably receive live prompt injection after it has started.
- Running intervention therefore uses `interrupt & retry`: terminate the process, record partial output, and run a child session.
- Streaming can vary by Gemini CLI version and output format.
- `ask_json` prioritizes final-response validation over streaming deltas.
- The UI shows the prompt MCP actually sent to Gemini and Gemini output. It does not expose Codex hidden reasoning or hidden system/developer instructions.
