from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .codex_install import (
    build_codex_config,
    build_mcp_env,
    build_uvx_options,
    write_user_config,
)
from .gemness_trigger import install as install_trigger
from .mcp_smoke import run_smoke
from .runner import resolve_agy_command as resolve_agy_command_parts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gemness", description="Gemness MCP command-line interface.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start-mcp-server", help="Start the Gemness MCP server over stdio.")

    bootstrap = subparsers.add_parser("bootstrap-codex", help="Install Gemness into Codex using a portable uvx MCP config.")
    bootstrap.add_argument(
        "--server-source",
        default=None,
        help="Remote git source for this MCP package, for example git+https://github.com/jisoq/gemness. Inferred when this CLI itself was installed from a git URL.",
    )
    bootstrap.add_argument("--python", default=None, help="Optional Python version for uvx, for example 3.11.")
    bootstrap.add_argument("--workspace-root", default=None, help="Optional workspace root Gemness may operate on. Omitted by default for portable config.")
    bootstrap.add_argument(
        "--allowed-root",
        action="append",
        default=[],
        help="Allowed workspace root. May be repeated. Defaults to --workspace-root.",
    )
    bootstrap.add_argument("--agy-command", default=None, help="Antigravity CLI command/path. Defaults to resolving agy from PATH.")
    bootstrap.add_argument("--skip-trigger", action="store_true", help="Do not install the gemness skill guidance.")
    bootstrap.add_argument("--skip-smoke-test", action="store_true", help="Write config without launching the configured MCP command.")
    bootstrap.add_argument("--smoke-timeout", type=float, default=60.0)

    trigger = subparsers.add_parser("install-trigger", help="Install or refresh the gemness skill guidance.")
    trigger.add_argument("--scope", choices=["project", "user", "both"], default="user")
    trigger.add_argument("--project-root", default=".")

    smoke = subparsers.add_parser("smoke-test", help="Smoke-test the MCP server over stdio.")
    smoke.add_argument("--real", action="store_true", help="Also call ask_antigravity and invoke Antigravity CLI.")
    smoke.add_argument("--timeout", type=float, default=10.0)
    smoke.add_argument("server_command", nargs=argparse.REMAINDER, help="Server command after --.")

    args = parser.parse_args(argv)
    if args.command == "start-mcp-server":
        from .server import main as server_main

        server_main()
    elif args.command == "bootstrap-codex":
        _bootstrap_codex(args)
    elif args.command == "install-trigger":
        for path in install_trigger(args.scope, Path(args.project_root)):
            print(f"updated {path}")
    elif args.command == "smoke-test":
        command = _normalize_command(args.server_command)
        if not command:
            raise SystemExit("Usage: gemness smoke-test [--real] -- gemness start-mcp-server")
        for line in run_smoke(command, real=args.real, timeout=args.timeout):
            print(line)


def _bootstrap_codex(args: argparse.Namespace) -> None:
    workspace_root = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else None
    allowed_roots = tuple(Path(root).expanduser().resolve() for root in args.allowed_root)
    source = args.server_source
    try:
        options = build_uvx_options(
            server_source=source,
            workspace_root=workspace_root,
            allowed_roots=allowed_roots,
            agy_command=args.agy_command,
            python=args.python,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    config = build_codex_config(options)
    config_path = write_user_config(config)
    print(f"updated {config_path}")
    print(f"mcp server name: gemness")
    print(f"mcp command: {options.command} {' '.join(options.args)}")
    print(f"workspace root: {options.workspace_root or '(not pinned)'}")
    print(f"allowed roots: {', '.join(str(root) for root in options.allowed_roots) or '(not pinned)'}")
    agy_command = options.agy_command or "agy"
    _check_agy_version(agy_command, workspace_root or Path.cwd())

    if not args.skip_trigger:
        for path in install_trigger("user", workspace_root or Path.cwd()):
            print(f"updated {path}")

    if not args.skip_smoke_test:
        command = [options.command, *options.args]
        for line in run_smoke(command, timeout=args.smoke_timeout, cwd=options.cwd or Path.cwd(), env=build_mcp_env(options)):
            print(line)

    print("restart Codex before using Gemness tools")
    print("try: use gemness health check")


def _check_agy_version(agy_command: str, cwd: Path) -> None:
    command_parts = resolve_agy_command_parts(agy_command)
    try:
        completed = subprocess.run(
            [*command_parts, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Antigravity CLI not found on PATH: {agy_command}") from exc
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(f"Antigravity CLI version check failed: {output or completed.returncode}")
    print(f"agy version: {output}")


def _normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


if __name__ == "__main__":
    main(sys.argv[1:])
