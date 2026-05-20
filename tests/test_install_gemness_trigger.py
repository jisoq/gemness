from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_gemness_trigger.py"
SPEC = importlib.util.spec_from_file_location("install_gemness_trigger", SCRIPT_PATH)
install_gemness_trigger = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(install_gemness_trigger)


def test_project_install_creates_agents_and_skill_when_missing(tmp_path) -> None:
    updated = install_gemness_trigger.install("project", tmp_path)

    agents = tmp_path / "AGENTS.md"
    skill = tmp_path / ".agents" / "skills" / "gemness" / "SKILL.md"
    assert updated == [agents, skill]
    assert "use gemness" in agents.read_text(encoding="utf-8")
    assert "name: gemness" in skill.read_text(encoding="utf-8")


def test_project_install_appends_without_overwriting_existing_content(tmp_path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Existing\n\nKeep me.\n", encoding="utf-8")

    install_gemness_trigger.install("project", tmp_path)

    text = agents.read_text(encoding="utf-8")
    assert "# Existing" in text
    assert "Keep me." in text
    assert text.count(install_gemness_trigger.START_MARKER) == 1


def test_project_install_replaces_existing_marker_block_without_duplicates(tmp_path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Existing\n\n"
        f"{install_gemness_trigger.START_MARKER}\nold block\n{install_gemness_trigger.END_MARKER}\n",
        encoding="utf-8",
    )

    install_gemness_trigger.install("project", tmp_path)
    install_gemness_trigger.install("project", tmp_path)

    text = agents.read_text(encoding="utf-8")
    assert "old block" not in text
    assert text.count(install_gemness_trigger.START_MARKER) == 1
    assert text.count(install_gemness_trigger.END_MARKER) == 1


def test_user_install_uses_temp_home_and_codex_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    install_gemness_trigger.install("user", tmp_path)

    assert (codex_home / "AGENTS.md").exists()
    assert (home / ".agents" / "skills" / "gemness" / "SKILL.md").exists()


def test_skill_front_matter_is_yamlish() -> None:
    text = install_gemness_trigger.SKILL_CONTENT
    assert text.startswith("---\n")
    assert "\n---\n\n# Gemness Skill" in text
    assert "antigravity reviewer" in text
    assert "advanced detached/background APIs" in text
    assert "//" not in text.split("---", 2)[1]
