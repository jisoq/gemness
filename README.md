# Gemness Observer

Local Gemness server with a browser observer UI for advisory Gemini calls.

The server exposes:

- `health_check`
- `ask_text`
- `follow_up`
- `ask_json`
- `review_current_diff`

Each root tool call creates a unique observer run. Use `follow_up` with a previous `session_id` to continue the same Gemini conversation; the Observer history groups those runs into one conversation entry. Tool results include `session_id`, `conversation_id`, and `observer_url`, so you can open the local UI and inspect prompts, Gemini output, parse/validation events, repair attempts, review findings, and intervention history.

## Quick Start

Gemness is designed for portable MCP installation: Codex launches it through `uvx` from a remote git source, so no machine depends on another machine's local checkout, `.venv`, or PyPI package name.

From any directory:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

The URL is used only by `uvx`; no PyPI token is needed.

The bootstrap command:

- writes the marked `[mcp_servers.gemness]` block to the user Codex config
- installs the `use gemness` trigger guidance
- checks the Gemini CLI version
- smoke-tests the configured MCP stdio command

Then restart Codex and ask:

```text
use gemness health check
```

The detailed route is in [INSTALL.md](INSTALL.md).

## Run

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness start-mcp-server
```

The server communicates over MCP stdio. By default, it starts the Observer web server as soon as the MCP process starts, so `http://127.0.0.1:56755` can be open before an `ask_text` call begins.

## Connect to Codex

1. Bootstrap the Codex config with `uvx`:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

2. Restart Codex. The generated MCP config uses:

- `command = "uvx"`
- `args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]`
- `default_tools_approval_mode = "prompt"`
- `GEMNESS_GEMINI_SKIP_TRUST = "false"` unless you explicitly choose to bypass Gemini CLI trust checks locally

### Windows example

```toml
command = "uvx"
args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]

[mcp_servers.gemness.env]
# Omit GEMNESS_MODEL to let Gemini CLI use its default model.
```

### macOS/Linux example

```toml
command = "uvx"
args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]

[mcp_servers.gemness.env]
# Omit GEMNESS_MODEL to let Gemini CLI use its default model.
```

## Verify in Codex

```powershell
codex mcp list
```

Then open Codex TUI, run `/mcp`, and confirm `gemness` is active. In a Codex chat, ask:

```text
use gemness: run health check
```

For a live Gemini second opinion:

```text
use gemness: give me a second opinion on this architecture
use gemness: review current diff
```

To install or refresh the `use gemness` trigger guidance:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness install-trigger --scope project
uvx --from git+https://github.com/jisoq/gemness gemness install-trigger --scope user
uvx --from git+https://github.com/jisoq/gemness gemness install-trigger --scope both
```

## Observer UI

Keep the live Observer open at:

```text
http://127.0.0.1:56755
```

That root page follows the newest running Gemness session, so you can watch the prompt, Gemini stream events, validation, repair, and final result while the MCP tool call is still running. Every successful tool invocation also returns an `observer_url` like:

```json
{
  "session_id": "2fc7...",
  "observer_url": "http://127.0.0.1:56755/"
}
```

Open that URL in a browser. The dashboard lists conversations and automatically follows the newest running one; you do not need to copy or remember session IDs. The UI shows:

- recent conversations with tool name, status, model, start time, duration, and turn count
- transcript events for prompt, Gemini response, JSON extraction, validation, repair, and final result
- redacted view by default, with an explicit raw toggle
- prompt edit, approve, cancel, interrupt-and-retry, follow-up, copy, and export controls

## Interventions

Set `GEMNESS_PAUSE_BEFORE_SEND=true` to pause sessions before sending prompts to Gemini. While queued or waiting for approval, the UI can edit the prompt, append an instruction, approve, or cancel.

During a running subprocess, the UI supports `interrupt and retry`. The current process is terminated, the partial output is recorded, and a child session is created with the original prompt, partial output, and user instruction.

Completed sessions support follow-up from the UI and the MCP `follow_up` tool. A child run is created with `parent_session_id`, kept under the same `conversation_id` when it extends the latest turn, and shown as the same conversation in Observer history.

## Security

- The observer binds only to `127.0.0.1`, `localhost`, or `::1`.
- The Observer binds to loopback only and uses `http://127.0.0.1:56755/` as the single local dashboard.
- API, SSE, export, and intervention endpoints are local loopback endpoints; they do not require a URL token.
- Transcripts are redacted by default in the UI and API.
- Raw transcript export requires an explicit `raw=1` request.
- Gemini is not given shell access. `review_current_diff` runs `git diff --no-color <base_ref> --` inside the MCP server and sends only the resulting diff text to Gemini.

## Environment

```bash
# GEMNESS_MODEL is optional. Omit it to let Gemini CLI use its default model.
GEMNESS_OBSERVER_ENABLED=true
GEMNESS_OBSERVER_HOST=127.0.0.1
GEMNESS_OBSERVER_PORT=56755
GEMNESS_OBSERVER_START_ON_INIT=true
GEMNESS_TRANSCRIPT_DIR=~/.gemness/transcripts
GEMNESS_REDACT_RAW_BY_DEFAULT=true
GEMNESS_PAUSE_BEFORE_SEND=false
GEMNESS_TOOL_TIMEOUT_SEC=600
GEMNESS_GEMINI_OUTPUT_FORMAT=stream-json
GEMNESS_GEMINI_SKIP_TRUST=false
GEMNESS_GEMINI_TRUST_WORKSPACE=true
GEMNESS_GEMINI_APPROVAL_MODE=plan
```

See [docs/gemini-observer.md](docs/gemini-observer.md) and [docs/codex-mcp-config.example.toml](docs/codex-mcp-config.example.toml).

## Tests

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
uv run python -m pytest -q -p no:cacheprovider
```
