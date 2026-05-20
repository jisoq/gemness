from __future__ import annotations

import argparse

from gemness.mcp_smoke import run_smoke


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the Gemness MCP server over stdio JSON-RPC.")
    parser.add_argument("--real", action="store_true", help="Also call ask_antigravity and invoke the real Antigravity CLI.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Server command after --, for example: -- gemness start-mcp-server")
    args = parser.parse_args()

    command = _normalize_command(args.command)
    if not command:
        raise SystemExit("Usage: python scripts/mcp_smoke_test.py [--real] -- gemness start-mcp-server")

    result = run_smoke(command, real=args.real, timeout=args.timeout)
    for line in result:
        print(line)


def _normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


if __name__ == "__main__":
    main()
