from __future__ import annotations

import os
import stat
from pathlib import Path

from gemness.config import GemnessConfig
from gemness.observer import ObserverHub


def test_event_creation_and_redacted_view(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model")
    hub.append_event(session.session_id, "prompt.rendered", "codex_mcp", {"prompt": "inline API_KEY=secret-value"})

    raw_events = hub.get_events(session.session_id, raw=True)
    redacted_events = hub.get_events(session.session_id, raw=False)

    assert raw_events[-1]["payload"]["prompt"] == "inline API_KEY=secret-value"
    assert redacted_events[-1]["payload"]["prompt"] == "inline API_KEY=[REDACTED]"
    assert (tmp_path / f"{session.session_id}.jsonl").exists()


def test_session_created_event_persists_dashboard_observer_url(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0))
    try:
        session = hub.create_session("ask_antigravity", "fake-model")
        raw_events = hub.get_events(session.session_id, raw=True)
        created = raw_events[0]["payload"]

        assert created["observer_url"].endswith("/")
        assert "token=" not in created["observer_url"]
        assert created["observer_path"] == "/"
        assert "token=" not in hub.observer_url(session.session_id)
        assert f"/sessions/{session.session_id}" not in hub.observer_url(session.session_id)
    finally:
        hub.shutdown()


def test_session_title_event_persists_for_future_sessions(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model", title="Live History 나누기")
    hub.set_title(session.session_id, "요청 기반 제목")

    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))

    assert restored.get_session(session.session_id)["title"] == "요청 기반 제목"


def test_observer_token_file_uses_private_mode_when_supported(tmp_path) -> None:
    ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    token_path = tmp_path / "observer-token.txt"

    assert token_path.exists()
    if os.name != "nt":
        mode = stat.S_IMODE(token_path.stat().st_mode)
        assert mode & 0o077 == 0


def test_session_state_transition(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity_json", "fake-model")
    hub.set_status(session.session_id, "running", "antigravity.started", {"model": "fake-model", "streaming": False})
    hub.set_status(session.session_id, "valid", "session.completed", {"result": {"status": "valid"}})

    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    assert restored.get_session(session.session_id)["status"] == "valid"
    assert restored.get_session(session.session_id)["duration_ms"] >= 0


def test_conversation_index_persists_run_mapping_before_process_output(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model", project_root=str(tmp_path))

    index_text = (tmp_path / "conversation-index.json").read_text(encoding="utf-8")
    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    restored_session = restored.get_session(session.session_id, raw=True)
    restored_conversation = restored.get_conversation(session.conversation_id, raw=True)

    assert session.session_id.startswith("run_")
    assert restored_session["run_id"] == session.session_id
    assert restored_session["conversation_id"] == session.conversation_id
    assert restored_session["stream_events_path"].endswith(f"{session.session_id}.jsonl")
    assert restored_conversation["current_agy_conversation_id"].startswith("gemness_")
    assert session.conversation_id in index_text


def test_agy_conversation_id_hidden_from_redacted_views(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model")

    redacted_session = hub.get_session(session.session_id, raw=False)
    redacted_events = hub.get_events(session.session_id, raw=False)

    assert "agy_conversation_id" not in redacted_session
    assert redacted_events[0]["payload"]["agy_conversation_id"] == "[REDACTED]"


def test_dashboard_hub_refreshes_sessions_written_by_another_process(tmp_path) -> None:
    dashboard = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    writer = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = writer.create_session("ask_antigravity", "fake-model", title="다른 프로세스 세션")
    writer.append_event(session.session_id, "prompt.sent", "codex_mcp", {"prompt": "hello"})

    sessions = dashboard.list_sessions()
    events = dashboard.get_events(session.session_id, raw=True)

    assert sessions[0]["session_id"] == session.session_id
    assert sessions[0]["title"] == "다른 프로세스 세션"
    assert [event["type"] for event in events][-1] == "prompt.sent"


def test_dashboard_refresh_marks_dead_started_process_as_error(tmp_path, monkeypatch) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model")
    hub.set_status(session.session_id, "running", "antigravity.started", {"pid": 999999})
    monkeypatch.setattr("gemness.observer._age_seconds", lambda updated_at, now: 60.0)
    monkeypatch.setattr("gemness.observer._process_is_running", lambda pid: False)

    listed = hub.list_sessions()[0]
    events = hub.get_events(session.session_id, raw=True)
    error_event = next(event for event in events if event["type"] == "session.error")

    assert listed["status"] == "error"
    assert error_event["payload"]["reason"] == "stale_observer_session"
    assert error_event["payload"]["status"] == "error"


def test_dashboard_refresh_marks_unupdated_open_session_as_error(tmp_path, monkeypatch) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False, agy_timeout_sec=10))
    session = hub.create_session("ask_antigravity", "fake-model")
    monkeypatch.setattr("gemness.observer._age_seconds", lambda updated_at, now: 30.0)

    listed = hub.list_sessions()[0]

    assert listed["session_id"] == session.session_id
    assert listed["status"] == "error"


def test_rename_conversation_updates_root_title_and_persists(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model", title="기존 제목")
    hub.rename_conversation(session.conversation_id, "새 대화 이름")

    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))

    assert restored.get_conversation(session.conversation_id)["title"] == "새 대화 이름"
    assert restored.get_session(session.session_id)["title"] == "새 대화 이름"


def test_delete_conversation_removes_terminal_runs_and_transcript_files(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model", title="삭제할 대화")
    artifact = hub.write_text_artifact(session.session_id, "stdout.txt", "raw output")
    extensionless_artifact = hub.write_text_artifact(session.session_id, "raw-output", "raw output")
    hub.set_status(session.session_id, "completed", "session.completed", {"result": {"status": "completed", "text": "done"}})

    result = hub.delete_conversation(session.conversation_id)
    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))

    assert result == {"conversation_id": session.conversation_id, "deleted_runs": 1}
    assert session.session_id not in [item["session_id"] for item in restored.list_sessions()]
    assert session.conversation_id not in [item["conversation_id"] for item in restored.list_conversations()]
    assert not (tmp_path / f"{session.session_id}.jsonl").exists()
    assert not Path(artifact["path"]).exists()
    assert not Path(extensionless_artifact["path"]).exists()


def test_follow_up_prompt_uses_summary_not_prior_turn_payloads(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_antigravity", "fake-model")
    hub.update_conversation_summary(session.conversation_id, "Prior decision: inspect the workspace directly.")
    hub.append_event(session.session_id, "prompt.sent", "codex_mcp", {"prompt": "Original prompt with a large pasted patch"})
    hub.append_event(session.session_id, "antigravity.response", "gemness", {"response": '{"response": "Original answer"}'})
    hub.set_status(session.session_id, "completed", "session.completed", {"result": {"status": "completed", "text": "Final answer"}})

    prompt = hub.build_follow_up_prompt(session.session_id, "Follow up please")

    assert "Context summary:" in prompt
    assert "Prior decision: inspect the workspace directly." in prompt
    assert "User follow-up:" in prompt
    assert "Follow up please" in prompt
    assert "Recent turns:" not in prompt
    assert "Original prompt" not in prompt
    assert "Original answer" not in prompt
    assert "Final answer" not in prompt
    assert "large pasted patch" not in prompt
    assert len(prompt) < 2000
