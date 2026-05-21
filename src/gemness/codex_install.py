from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

from .config import DEFAULT_OBSERVER_PORT
from .config import DEFAULT_TRANSCRIPT_DIR


START_MARKER = "# gemness-mcp:start"
END_MARKER = "# gemness-mcp:end"
MCP_SERVER_NAME = "gemness"
PACKAGE_NAME = "gemness"
CONSOLE_SCRIPT = "gemness"
TOOL_NAMES = (
    "antigravity_health",
    "ask_antigravity",
    "start_antigravity",
    "follow_up_antigravity",
    "start_follow_up_antigravity",
    "ask_antigravity_json",
    "start_antigravity_json",
    "review_current_diff_with_antigravity",
    "start_review_current_diff_with_antigravity",
    "get_antigravity_run",
    "await_antigravity_run",
    "cancel_antigravity_run",
)
AUTO_APPROVE_TOOLS = {
    "antigravity_health",
    "ask_antigravity",
    "follow_up_antigravity",
    "get_antigravity_run",
    "await_antigravity_run",
}


@dataclass(frozen=True, slots=True)
class CodexConfigOptions:
    command: str
    args: tuple[str, ...]
    cwd: Path | None
    workspace_root: Path | None
    allowed_roots: tuple[Path, ...]
    agy_command: str | None
    startup_timeout_sec: int = 60
    tool_timeout_sec: int = 600
    required: bool = False
    transcript_dir: str = str(DEFAULT_TRANSCRIPT_DIR)


def build_uvx_options(
    *,
    server_source: str | None,
    workspace_root: Path | None,
    allowed_roots: tuple[Path, ...],
    agy_command: str | None = None,
    python: str | None = None,
) -> CodexConfigOptions:
    source = resolve_server_source(server_source)
    args: list[str] = []
    if python:
        args.extend(("-p", python))
    args.extend(("--from", source, CONSOLE_SCRIPT, "start-mcp-server"))
    resolved_workspace = workspace_root.expanduser().resolve() if workspace_root else None
    resolved_allowed = tuple(root.expanduser().resolve() for root in allowed_roots)
    return CodexConfigOptions(
        command="uvx",
        args=tuple(args),
        cwd=resolved_workspace,
        workspace_root=resolved_workspace,
        allowed_roots=resolved_allowed,
        agy_command=agy_command,
    )


def build_codex_config(options: CodexConfigOptions) -> str:
    cwd_line = f"cwd = {_toml_string(options.cwd)}\n" if options.cwd else ""
    tool_lines = "\n".join(f"  {_toml_string(name)}," for name in TOOL_NAMES)
    approval_blocks = "\n\n".join(
        (
            f"[mcp_servers.{MCP_SERVER_NAME}.tools.{_toml_string(name)}]\n"
            f"approval_mode = {_toml_string('approve' if name in AUTO_APPROVE_TOOLS else 'prompt')}"
        )
        for name in TOOL_NAMES
    )
    env_lines = {
        "GEMNESS_OBSERVER_ENABLED": "true",
        "GEMNESS_OBSERVER_HOST": "127.0.0.1",
        "GEMNESS_OBSERVER_PORT": str(DEFAULT_OBSERVER_PORT),
        "GEMNESS_OBSERVER_START_ON_INIT": "true",
        "GEMNESS_TRANSCRIPT_DIR": options.transcript_dir,
        "GEMNESS_REDACT_RAW_BY_DEFAULT": "true",
        "GEMNESS_AGY_TIMEOUT": "600",
        "GEMNESS_AGY_CAPTURE_MODE": "auto",
        "GEMNESS_AGY_HEARTBEAT_INTERVAL": "5",
        "GEMNESS_AGY_CONCURRENCY_LIMIT": "4",
    }
    if options.agy_command:
        env_lines["GEMNESS_AGY_COMMAND"] = options.agy_command
    if options.workspace_root:
        env_lines["GEMNESS_WORKSPACE_ROOT"] = str(options.workspace_root)
    if options.allowed_roots:
        env_lines["GEMNESS_ALLOWED_ROOTS"] = os.pathsep.join(str(root) for root in options.allowed_roots)
    env_body = "\n".join(f"{name} = {_toml_string(value)}" for name, value in env_lines.items())
    return f"""{START_MARKER}
[mcp_servers.{MCP_SERVER_NAME}]
command = {_toml_string(options.command)}
args = {_toml_array(options.args)}
{cwd_line}startup_timeout_sec = {options.startup_timeout_sec}
tool_timeout_sec = {options.tool_timeout_sec}
required = {_toml_bool(options.required)}
enabled_tools = [
{tool_lines}
]
default_tools_approval_mode = "prompt"

{approval_blocks}

[mcp_servers.{MCP_SERVER_NAME}.env]
{env_body}
{END_MARKER}"""


def build_mcp_env(options: CodexConfigOptions, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env.update(
        {
            "GEMNESS_OBSERVER_ENABLED": "true",
            "GEMNESS_OBSERVER_HOST": "127.0.0.1",
            "GEMNESS_OBSERVER_PORT": str(DEFAULT_OBSERVER_PORT),
            "GEMNESS_OBSERVER_START_ON_INIT": "true",
            "GEMNESS_TRANSCRIPT_DIR": options.transcript_dir,
            "GEMNESS_REDACT_RAW_BY_DEFAULT": "true",
            "GEMNESS_AGY_TIMEOUT": "600",
            "GEMNESS_AGY_CAPTURE_MODE": "auto",
            "GEMNESS_AGY_HEARTBEAT_INTERVAL": "5",
            "GEMNESS_AGY_CONCURRENCY_LIMIT": "4",
        }
    )
    if options.agy_command:
        env["GEMNESS_AGY_COMMAND"] = options.agy_command
    if options.workspace_root:
        env["GEMNESS_WORKSPACE_ROOT"] = str(options.workspace_root)
    if options.allowed_roots:
        env["GEMNESS_ALLOWED_ROOTS"] = os.pathsep.join(str(root) for root in options.allowed_roots)
    return env


def resolve_server_source(server_source: str | None = None) -> str:
    source = (server_source or infer_remote_server_source() or "").strip()
    if not source:
        raise RuntimeError(
            "Gemness MCP source is required. Run bootstrap through "
            "`uvx --from git+https://... gemness bootstrap-codex`, or pass "
            "`--server-source git+https://...`."
        )
    if _is_local_source(source):
        raise RuntimeError("Local Gemness MCP sources are not allowed for bootstrap. Use a remote git URL such as git+https://github.com/jisoq/gemness.")
    if not _is_remote_git_source(source):
        raise RuntimeError("Gemness MCP source must be a remote git URL, for example git+https://github.com/jisoq/gemness.")
    return source


def infer_remote_server_source() -> str | None:
    try:
        dist = metadata.distribution(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return None
    direct_url = dist.read_text("direct_url.json")
    if not direct_url:
        return None
    data = json.loads(direct_url)
    vcs = data.get("vcs_info") or {}
    raw_url = str(data.get("url") or "").strip()
    if vcs.get("vcs") != "git" or not _is_remote_git_url(raw_url):
        return None
    revision = vcs.get("requested_revision") or vcs.get("commit_id")
    suffix = f"@{revision}" if revision else ""
    return raw_url if raw_url.startswith("git+") else f"git+{raw_url}{suffix}"


def write_user_config(config: str) -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    path = codex_home / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(upsert_marked_block(existing, config, START_MARKER, END_MARKER), encoding="utf-8")
    return path


def upsert_marked_block(existing: str, block: str, start_marker: str, end_marker: str) -> str:
    block = block.rstrip()
    if start_marker in existing and end_marker in existing:
        start = existing.index(start_marker)
        end = existing.index(end_marker, start) + len(end_marker)
        return (existing[:start].rstrip() + "\n\n" + block + existing[end:].rstrip() + "\n").lstrip()
    if start_marker in existing:
        start = existing.index(start_marker)
        existing = existing[:start].rstrip() + "\n"
    if end_marker in existing:
        end = existing.index(end_marker) + len(end_marker)
        existing = (existing[: existing.index(end_marker)].rstrip() + existing[end:].rstrip() + "\n").lstrip()
    if existing.strip():
        return existing.rstrip() + "\n\n" + block + "\n"
    return block + "\n"


def _is_remote_git_source(source: str) -> bool:
    if not source.startswith(("git+https://", "git+ssh://")):
        return False
    return _is_remote_git_url(source.removeprefix("git+"))


def _is_remote_git_url(url: str) -> bool:
    parsed = urlparse(url.removeprefix("git+"))
    return parsed.scheme in {"https", "ssh"} and bool(parsed.netloc)


def _is_local_source(source: str) -> bool:
    if source.startswith(("file:", ".", "~")):
        return True
    return Path(source).expanduser().is_absolute()


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
