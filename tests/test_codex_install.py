from __future__ import annotations

import tomllib
import pytest
from gemness.codex_install import (
    build_codex_config,
    build_mcp_env,
    build_uvx_options,
    resolve_server_source,
)


def test_build_uvx_config_uses_gemness_server_name(tmp_path) -> None:
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(tmp_path,),
        gemini_command="gemini",
    )
    parsed = tomllib.loads(build_codex_config(options))

    assert set(parsed["mcp_servers"]) == {"gemness"}
    server = parsed["mcp_servers"]["gemness"]
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", "git+https://example.test/gemness", "gemness", "start-mcp-server"]
    assert server["cwd"] == str(tmp_path.resolve())
    assert server["required"] is False
    assert "health_check" in server["enabled_tools"]
    assert server["env"]["GEMNESS_ALLOWED_ROOTS"] == str(tmp_path.resolve())


def test_build_uvx_config_requires_remote_source_when_not_git_installed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="source is required|remote git URL"):
        build_uvx_options(
            server_source=None,
            workspace_root=tmp_path,
            allowed_roots=(tmp_path,),
            gemini_command="gemini",
        )


def test_local_source_is_rejected(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="Local Gemness MCP sources are not allowed"):
        resolve_server_source(str(tmp_path))


def test_pypi_name_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="remote git URL"):
        resolve_server_source("gemness")


def test_build_uvx_config_allows_python_pin(tmp_path) -> None:
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(tmp_path,),
        gemini_command="gemini",
        python="3.11",
    )

    assert options.args[:4] == ("-p", "3.11", "--from", "git+https://example.test/gemness")


def test_build_mcp_env_matches_workspace_and_allowed_roots(tmp_path) -> None:
    other = tmp_path / "other"
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(tmp_path, other),
        gemini_command="gemini",
    )
    env = build_mcp_env(options, {"EXISTING": "1"})

    assert env["EXISTING"] == "1"
    assert env["GEMNESS_WORKSPACE_ROOT"] == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in env["GEMNESS_ALLOWED_ROOTS"]
    assert str(other.resolve()) in env["GEMNESS_ALLOWED_ROOTS"]
