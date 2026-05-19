# Gemness Observer

Local Gemness server with a browser observer UI for advisory Gemini calls.

The server exposes:

- `health_check`
- `ask_text`
- `ask_json`
- `review_current_diff`

Each tool call creates a unique observer session. Tool results include `session_id` and `observer_url`, so you can open the local UI and inspect prompts, Gemini output, parse/validation events, repair attempts, review findings, and intervention history.

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

The server communicates over MCP stdio. It starts the observer web server lazily on the first Gemini tool call.

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
GEMNESS_MODEL = "gemini-3.1-pro-preview"
```

### macOS/Linux example

```toml
command = "uvx"
args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]

[mcp_servers.gemness.env]
GEMNESS_MODEL = "gemini-3.1-pro-preview"
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

Every successful tool invocation returns an `observer_url` like:

```json
{
  "session_id": "2fc7...",
  "observer_url": "http://127.0.0.1:PORT/sessions/2fc7...?token=LOCAL_TOKEN"
}
```

Open that URL in a browser. The UI shows:

- recent sessions with tool name, status, model, start time, and duration
- transcript events for prompt, Gemini response, JSON extraction, validation, repair, and final result
- redacted view by default, with an explicit raw toggle
- prompt edit, approve, cancel, interrupt-and-retry, follow-up, copy, and export controls

## Interventions

Set `GEMNESS_PAUSE_BEFORE_SEND=true` to pause sessions before sending prompts to Gemini. While queued or waiting for approval, the UI can edit the prompt, append an instruction, approve, or cancel.

During a running subprocess, the UI supports `interrupt and retry`. The current process is terminated, the partial output is recorded, and a child session is created with the original prompt, partial output, and user instruction.

Completed sessions support follow-up. A child session is created with `parent_session_id` and a redacted transcript summary.

## Security

- The observer binds only to `127.0.0.1`, `localhost`, or `::1`.
- All `/api/*`, SSE, export, and intervention endpoints require the random local token.
- Transcripts are redacted by default in the UI and API.
- Raw transcript export requires the token and an explicit `raw=1` request.
- Gemini is not given shell access. `review_current_diff` runs `git diff --no-color <base_ref> --` inside the MCP server and sends only the resulting diff text to Gemini.

## Environment

```bash
GEMNESS_MODEL=gemini-3.1-pro-preview
GEMNESS_OBSERVER_ENABLED=true
GEMNESS_OBSERVER_HOST=127.0.0.1
GEMNESS_OBSERVER_PORT=0
GEMNESS_TRANSCRIPT_DIR=.gemness/transcripts
GEMNESS_REDACT_RAW_BY_DEFAULT=true
GEMNESS_PAUSE_BEFORE_SEND=false
GEMNESS_TOOL_TIMEOUT_SEC=120
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
