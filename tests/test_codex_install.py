from __future__ import annotations

import tomllib
from pathlib import Path

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
        workspace_root=None,
        allowed_roots=(),
        agy_command=None,
    )
    parsed = tomllib.loads(build_codex_config(options))

    assert set(parsed["mcp_servers"]) == {"gemness"}
    server = parsed["mcp_servers"]["gemness"]
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", "git+https://example.test/gemness", "gemness", "start-mcp-server"]
    assert "cwd" not in server
    assert server["tool_timeout_sec"] == 600
    assert server["required"] is False
    assert "antigravity_health" in server["enabled_tools"]
    assert "ask_antigravity" in server["enabled_tools"]
    assert "start_antigravity" in server["enabled_tools"]
    assert "await_antigravity_run" in server["enabled_tools"]
    assert "cancel_antigravity_run" in server["enabled_tools"]
    assert "start_antigravity_json" not in server["enabled_tools"]
    assert "start_review_current_diff_with_antigravity" not in server["enabled_tools"]
    assert "start_follow_up_antigravity" not in server["enabled_tools"]
    assert "get_antigravity_run" not in server["enabled_tools"]
    assert server["env"]["GEMNESS_OBSERVER_PORT"] == "56755"
    assert server["env"]["GEMNESS_OBSERVER_START_ON_INIT"] == "true"
    assert Path(server["env"]["GEMNESS_TRANSCRIPT_DIR"]).is_absolute()
    assert server["env"]["GEMNESS_AGY_TIMEOUT"] == "600"
    assert server["env"]["GEMNESS_AGY_CAPTURE_MODE"] == "winpty"
    assert server["env"]["GEMNESS_AGY_HEARTBEAT_INTERVAL"] == "5"
    assert server["env"]["GEMNESS_AGY_CONCURRENCY_LIMIT"] == "4"
    assert server["tools"]["start_antigravity"]["approval_mode"] == "prompt"
    assert server["tools"]["cancel_antigravity_run"]["approval_mode"] == "prompt"
    assert server["tools"]["ask_antigravity"]["approval_mode"] == "approve"
    assert "GEMNESS_AGY_COMMAND" not in server["env"]
    assert "GEMNESS_WORKSPACE_ROOT" not in server["env"]
    assert "GEMNESS_ALLOWED_ROOTS" not in server["env"]


def test_build_uvx_config_can_pin_workspace_when_explicit(tmp_path) -> None:
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(tmp_path,),
        agy_command="agy",
    )
    parsed = tomllib.loads(build_codex_config(options))

    server = parsed["mcp_servers"]["gemness"]
    assert server["cwd"] == str(tmp_path.resolve())
    assert server["env"]["GEMNESS_WORKSPACE_ROOT"] == str(tmp_path.resolve())
    assert server["env"]["GEMNESS_ALLOWED_ROOTS"] == str(tmp_path.resolve())
    assert server["env"]["GEMNESS_AGY_COMMAND"] == "agy"


def test_workspace_root_without_allowed_root_is_implicit_not_strict(tmp_path) -> None:
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(),
        agy_command=None,
    )
    parsed = tomllib.loads(build_codex_config(options))

    server = parsed["mcp_servers"]["gemness"]
    assert server["cwd"] == str(tmp_path.resolve())
    assert server["env"]["GEMNESS_WORKSPACE_ROOT"] == str(tmp_path.resolve())
    assert "GEMNESS_ALLOWED_ROOTS" not in server["env"]


def test_build_uvx_config_requires_remote_source_when_not_git_installed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="source is required|remote git URL"):
        build_uvx_options(
            server_source=None,
            workspace_root=tmp_path,
            allowed_roots=(tmp_path,),
            agy_command="agy",
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
        agy_command="agy",
        python="3.11",
    )

    assert options.args[:4] == ("-p", "3.11", "--from", "git+https://example.test/gemness")


def test_build_mcp_env_matches_workspace_and_allowed_roots(tmp_path) -> None:
    other = tmp_path / "other"
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=tmp_path,
        allowed_roots=(tmp_path, other),
        agy_command="agy",
    )
    env = build_mcp_env(options, {"EXISTING": "1"})

    assert env["EXISTING"] == "1"
    assert env["GEMNESS_WORKSPACE_ROOT"] == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in env["GEMNESS_ALLOWED_ROOTS"]
    assert str(other.resolve()) in env["GEMNESS_ALLOWED_ROOTS"]
    assert env["GEMNESS_AGY_COMMAND"] == "agy"


def test_build_mcp_env_omits_local_paths_by_default() -> None:
    options = build_uvx_options(
        server_source="git+https://example.test/gemness",
        workspace_root=None,
        allowed_roots=(),
        agy_command=None,
    )
    env = build_mcp_env(options, {"EXISTING": "1"})

    assert env["EXISTING"] == "1"
    assert env["GEMNESS_OBSERVER_PORT"] == "56755"
    assert env["GEMNESS_OBSERVER_START_ON_INIT"] == "true"
    assert Path(env["GEMNESS_TRANSCRIPT_DIR"]).is_absolute()
    assert env["GEMNESS_AGY_TIMEOUT"] == "600"
    assert env["GEMNESS_AGY_CAPTURE_MODE"] == "winpty"
    assert env["GEMNESS_AGY_HEARTBEAT_INTERVAL"] == "5"
    assert env["GEMNESS_AGY_CONCURRENCY_LIMIT"] == "4"
    assert "GEMNESS_AGY_COMMAND" not in env
    assert "GEMNESS_WORKSPACE_ROOT" not in env
    assert "GEMNESS_ALLOWED_ROOTS" not in env


def test_upsert_marked_block_removes_orphan_end_marker() -> None:
    from gemness.codex_install import END_MARKER, START_MARKER, upsert_marked_block

    result = upsert_marked_block("before\n# gemness-mcp:end\n", "BLOCK", START_MARKER, END_MARKER)

    assert result == "before\n\nBLOCK\n"
