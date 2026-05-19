from __future__ import annotations

import os
import stat

from gemness.config import GemnessConfig
from gemness.observer import ObserverHub


def test_event_creation_and_redacted_view(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    hub.append_event(session.session_id, "prompt.rendered", "codex_mcp", {"prompt": "inline API_KEY=secret-value"})

    raw_events = hub.get_events(session.session_id, raw=True)
    redacted_events = hub.get_events(session.session_id, raw=False)

    assert raw_events[-1]["payload"]["prompt"] == "inline API_KEY=secret-value"
    assert redacted_events[-1]["payload"]["prompt"] == "inline API_KEY=[REDACTED]"
    assert (tmp_path / f"{session.session_id}.jsonl").exists()


def test_session_created_event_persists_dashboard_observer_url(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0))
    try:
        session = hub.create_session("ask_text", "fake-model")
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
    session = hub.create_session("ask_text", "fake-model", title="Live History 나누기")
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
    session = hub.create_session("ask_json", "fake-model")
    hub.set_status(session.session_id, "running", "gemini.started", {"model": "fake-model"})
    hub.set_status(session.session_id, "valid", "session.completed", {"result": {"status": "valid"}})

    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    assert restored.get_session(session.session_id)["status"] == "valid"
    assert restored.get_session(session.session_id)["duration_ms"] >= 0


def test_conversation_index_persists_run_mapping_before_process_output(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model", project_root=str(tmp_path))

    index_text = (tmp_path / "conversation-index.json").read_text(encoding="utf-8")
    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    restored_session = restored.get_session(session.session_id, raw=True)
    restored_conversation = restored.get_conversation(session.conversation_id, raw=True)

    assert session.session_id.startswith("run_")
    assert restored_session["run_id"] == session.session_id
    assert restored_session["conversation_id"] == session.conversation_id
    assert restored_session["stream_events_path"].endswith(f"{session.session_id}.jsonl")
    assert restored_conversation["current_gemini_session_id"].startswith("gemness_")
    assert session.conversation_id in index_text


def test_gemini_session_id_hidden_from_redacted_views(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")

    redacted_session = hub.get_session(session.session_id, raw=False)
    redacted_events = hub.get_events(session.session_id, raw=False)

    assert "gemini_session_id" not in redacted_session
    assert redacted_events[0]["payload"]["gemini_session_id"] == "[REDACTED]"


def test_dashboard_hub_refreshes_sessions_written_by_another_process(tmp_path) -> None:
    dashboard = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    writer = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = writer.create_session("ask_text", "fake-model", title="다른 프로세스 세션")
    writer.append_event(session.session_id, "prompt.sent", "codex_mcp", {"prompt": "hello"})

    sessions = dashboard.list_sessions()
    events = dashboard.get_events(session.session_id, raw=True)

    assert sessions[0]["session_id"] == session.session_id
    assert sessions[0]["title"] == "다른 프로세스 세션"
    assert [event["type"] for event in events][-1] == "prompt.sent"


def test_intervention_queue_records_received_and_applied(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    hub.add_intervention(session.session_id, "edit_prompt", prompt="edited")
    intervention = hub.pop_intervention(session.session_id, {"edit_prompt"})

    assert intervention is not None
    assert intervention.prompt == "edited"
    event_types = [event["type"] for event in hub.get_events(session.session_id, raw=True)]
    assert "intervention.received" in event_types
    assert "intervention.applied" in event_types


def test_restore_ignores_intervention_status_values(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    hub.set_status(session.session_id, "completed", "session.completed", {"result": {"status": "completed", "text": "done"}})
    hub.add_intervention(session.session_id, "approve", instruction="follow up")
    hub.pop_intervention(session.session_id, {"approve"})

    restored = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))

    assert restored.get_session(session.session_id)["status"] == "completed"


def test_follow_up_prompt_uses_readable_summary_not_full_json_transcript(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=False))
    session = hub.create_session("ask_text", "fake-model")
    hub.append_event(session.session_id, "prompt.sent", "codex_mcp", {"prompt": "Original prompt"})
    hub.append_event(session.session_id, "gemini.response", "gemness", {"response": '{"response": "Original answer"}'})
    hub.set_status(session.session_id, "completed", "session.completed", {"result": {"status": "completed", "text": "Final answer"}})

    prompt = hub.build_follow_up_prompt(session.session_id, "Follow up please")

    assert "Conversation summary:" in prompt
    assert "Recent turns:" in prompt
    assert "Original prompt" in prompt
    assert "Original answer" in prompt
    assert "Final answer" in prompt
    assert "New user request:" in prompt
    assert "Follow up please" in prompt
    assert len(prompt) < 5000
