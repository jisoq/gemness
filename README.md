# Gemness Observer

Gemness is a local MCP server that lets Codex consult Antigravity CLI (`agy`) and inspect each advisory run in a browser Observer UI.

The server exposes:

- `antigravity_health`
- `ask_antigravity`
- `follow_up_antigravity`
- `ask_antigravity_json`
- `review_current_diff_with_antigravity`

Each root tool call creates a Gemness run and a Gemness conversation. Gemness sends concise task instructions to `agy` and lets Antigravity inspect the workspace with its own tools when needed. Follow-up calls use native `agy --conversation <id>` when Gemness has detected the Antigravity conversation ID, otherwise they can fall back to `agy --continue` or a short summary prompt. Tool results include `run_id`, `conversation_id`, `observer_url`, and non-streaming execution metadata.

## Quick Start

Gemness is designed for portable MCP installation: Codex launches it through `uvx` from a remote git source, so no machine depends on another machine's local checkout, `.venv`, or PyPI package name.

From any directory:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

The bootstrap command:

- writes the marked `[mcp_servers.gemness]` block to the user Codex config
- installs the `use gemness` trigger guidance
- checks `agy --version`
- smoke-tests the configured MCP stdio command

Then restart Codex and ask:

```text
use gemness health check
```

The detailed route is in [INSTALL.md](INSTALL.md).

## Antigravity CLI

Install Antigravity CLI from the official docs:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

Smoke-test the CLI:

```powershell
agy --help
agy -p "Return exactly: GEMNESS_AGY_HEALTHCHECK"
```

On Windows, Gemness resolves `agy` from `PATH` first and then checks `%LOCALAPPDATA%\agy\bin\agy.exe`. Antigravity CLI can write print-mode responses directly to the console instead of stdout/stderr on Windows; Gemness uses `pywinpty` automatically there so MCP tools can capture the final text. Set `GEMNESS_AGY_CAPTURE_MODE=pipe` only when you need to force ordinary stdout/stderr capture.

Model selection belongs to Antigravity CLI, not Gemness runtime flags. Use Antigravity CLI settings or the `/model` command. A target such as `Gemini 3.5 Flash` is a user-facing Antigravity model preference, not a Gemness `--model` argument.

## Run

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness start-mcp-server
```

The server communicates over MCP stdio. By default, it starts the Observer web server as soon as the MCP process starts, so `http://127.0.0.1:56755` can be open before an `ask_antigravity` call begins.

## Connect To Codex

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

The generated MCP config uses:

- `command = "uvx"`
- `args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]`
- `default_tools_approval_mode = "prompt"`
- `GEMNESS_AGY_TIMEOUT = "600"`

To use a specific CLI path:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex --agy-command "$env:LOCALAPPDATA\agy\bin\agy.exe"
```

## Observer UI

Keep the live Observer open at:

```text
http://127.0.0.1:56755
```

The dashboard lists conversations and follows the newest running one. It shows prompts, final Antigravity output, stderr diagnostics, JSON extraction, validation, repair attempts, and review findings. You can rename or remove completed local conversation records from the session list. Antigravity output is captured after process completion; metadata is marked with `streaming=false`.

## Conversation Management

Use the MCP `follow_up_antigravity` tool to continue a completed run. The Observer UI itself is read-mostly: it does not edit queued prompts, interrupt running subprocesses, or create follow-up runs. Session-list actions are limited to local transcript housekeeping such as renaming and removing completed records.

## Security

- The observer binds only to `127.0.0.1`, `localhost`, or `::1`.
- API, SSE, export, rename, and delete endpoints are local loopback endpoints.
- Transcripts are redacted by default in the UI and API.
- Raw transcript export requires an explicit `raw=1` request.
- Gemness should not be used as a bulk context courier. Do not paste diffs, file dumps, logs, or transcript payloads into prompts when Antigravity can inspect the workspace itself.
- `review_current_diff_with_antigravity` does not embed a Gemness-generated diff. It starts `agy` in the requested workspace and asks Antigravity CLI to inspect the repository changes itself. Do not use it on workspaces containing secrets you would not want the local Antigravity CLI to inspect.

## Environment

```bash
GEMNESS_AGY_COMMAND=agy
GEMNESS_AGY_TIMEOUT=600
GEMNESS_AGY_HEALTH_TIMEOUT=20
GEMNESS_AGY_CAPTURE_MODE=auto
GEMNESS_OBSERVER_ENABLED=true
GEMNESS_OBSERVER_HOST=127.0.0.1
GEMNESS_OBSERVER_PORT=56755
GEMNESS_OBSERVER_START_ON_INIT=true
GEMNESS_TRANSCRIPT_DIR=~/.gemness/transcripts
GEMNESS_REDACT_RAW_BY_DEFAULT=true
```

See [docs/antigravity-observer.md](docs/antigravity-observer.md) and [docs/codex-mcp-config.example.toml](docs/codex-mcp-config.example.toml).

## Tests

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -p no:cacheprovider
```
