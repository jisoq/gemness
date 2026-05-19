# Install Gemness MCP For Yourself

This is the quick-start route for installing Gemness into Codex on any local machine.

## Contract

- Configure Codex to launch the `gemness` MCP server with `uvx`.
- Never use a local checkout path or a PyPI package-name fallback as the MCP package source.
- Preserve unrelated Codex config.
- Do not store secrets, API keys, or raw `.env` values in the MCP config.
- Do not start a long-running standalone Observer server. Codex starts the MCP server over stdio, and the Observer UI starts lazily on the first tool call.
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

Resolve Gemini CLI:

Windows PowerShell:

```powershell
Get-Command gemini | Select-Object -ExpandProperty Source
gemini --version
```

macOS/Linux:

```bash
command -v gemini
gemini --version
```

If Gemini CLI is missing or not authenticated, stop and report that blocker. Do not pretend Gemness is connected.

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

It also installs the `use gemness` trigger guidance for future agent sessions and runs an MCP stdio smoke test.

## 3. What Bootstrap Writes

The generated Codex config uses a portable launch command:

```toml
[mcp_servers.gemness]
command = "uvx"
args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]
startup_timeout_sec = 60
tool_timeout_sec = 300
required = false
```

Verify:

- `command = "uvx"`.
- `args` runs `gemness start-mcp-server`.
- Default bootstrap does not write `cwd`.
- Default bootstrap does not write workspace-root or allowed-root environment values.
- Default bootstrap does not write a machine-specific `GEMNESS_COMMAND`; Gemness resolves `gemini` from `PATH`.
- `GEMNESS_GEMINI_TRUST_WORKSPACE = "true"` is present.

## 4. Final Report

Report these items back to the user:

- Codex config path updated.
- MCP server name: `gemness`.
- MCP launch command, especially the `uvx --from ... gemness start-mcp-server` source.
- Gemini CLI command and version.
- Smoke-test result.
- Whether Codex must be restarted before `gemness` tools appear.
- The first phrase to try after restart: `use gemness health check`.
