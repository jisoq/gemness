# Install Gemness MCP For Yourself

This is the quick-start route for installing Gemness into Codex on any local machine.

## Contract

- Configure Codex to launch the `gemness` MCP server with `uvx`.
- Use Antigravity CLI (`agy`) as the only model backend.
- Never use a local checkout path or a PyPI package-name fallback as the MCP package source.
- Preserve unrelated Codex config.
- Do not store secrets, API keys, or raw `.env` values in the MCP config.
- Verify the install through MCP stdio before calling it done.

## 1. Prerequisites

Install `uv` and make sure it is on `PATH`.

Windows PowerShell:

```powershell
winget install --id=astral-sh.uv -e
uv --version
```

macOS/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

Install Antigravity CLI from the official installer.

Windows PowerShell:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy --help
agy -p "Return exactly: GEMNESS_AGY_HEALTHCHECK"
```

macOS/Linux:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy --help
agy -p "Return exactly: GEMNESS_AGY_HEALTHCHECK"
```

If `agy` is missing, Gemness on Windows also checks `%LOCALAPPDATA%\agy\bin\agy.exe`. If authentication is required, run `agy` once and complete the browser sign-in flow. Windows installs include `pywinpty` so Gemness can capture Antigravity print-mode text even when the CLI writes directly to the console instead of stdout/stderr.

## 2. Bootstrap Codex

From any directory, run:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

This installs from GitHub with `uvx` and does not require a PyPI token.

The bootstrap command writes or replaces only the marked block between:

```text
# gemness-mcp:start
# gemness-mcp:end
```

It also installs or updates the `gemness` skill guidance and runs an MCP stdio smoke test.

## 3. What Bootstrap Writes

The generated Codex config uses a portable launch command:

```toml
[mcp_servers.gemness]
command = "uvx"
args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]
startup_timeout_sec = 60
tool_timeout_sec = 600
required = false

[mcp_servers.gemness.env]
GEMNESS_AGY_TIMEOUT = "600"
GEMNESS_AGY_CAPTURE_MODE = "auto"
GEMNESS_AGY_HEARTBEAT_INTERVAL = "5"
GEMNESS_AGY_CONCURRENCY_LIMIT = "4"
```

Verify:

- `command = "uvx"`.
- `args` runs `gemness start-mcp-server`.
- Default bootstrap does not write `cwd`.
- Default bootstrap does not write workspace-root or allowed-root environment values.
- Default bootstrap does not write a machine-specific `GEMNESS_AGY_COMMAND`.
- `--workspace-root` sets a default cwd and implicit root; it does not enable strict allowlist mode by itself.
- `--allowed-root` writes `GEMNESS_ALLOWED_ROOTS` and enables strict explicit allowlist mode, which disables Codex trusted-project automatic mode.
- `GEMNESS_AGY_CAPTURE_MODE = "auto"` allows Windows console capture and ordinary stdout/stderr capture elsewhere.
- `GEMNESS_AGY_HEARTBEAT_INTERVAL = "5"` records progress events for long detached runs.
- `GEMNESS_AGY_CONCURRENCY_LIMIT = "4"` limits concurrent Antigravity background runs.

To pin a local CLI path explicitly:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex --agy-command "$env:LOCALAPPDATA\agy\bin\agy.exe"
```

## 4. Antigravity CLI MCP Notes

Codex MCP installation is the primary Gemness target. If you also configure an Antigravity CLI MCP client manually, keep that configuration separate from Codex TOML.

Workspace-local Antigravity CLI MCP config:

```json
{
  "mcpServers": {
    "gemness": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]
    }
  }
}
```

Place it at `.agents/mcp_config.json` for a workspace, or at `~/.gemini/antigravity-cli/mcp_config.json` for the Antigravity CLI global config.

Remote MCP examples for Antigravity CLI use `serverUrl`:

```json
{
  "mcpServers": {
    "remote-example": {
      "serverUrl": "https://example.test/mcp"
    }
  }
}
```

## 5. Final Report

Report these items back to the user:

- Codex config path updated.
- MCP server name: `gemness`.
- MCP launch command, especially the `uvx --from ... gemness start-mcp-server` source.
- Antigravity CLI command and version.
- Smoke-test result.
- Whether Codex must be restarted before `gemness` tools appear.
- The first phrase to try after restart: `use gemness health check`.
