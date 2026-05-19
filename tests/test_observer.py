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


def test_session_created_event_persists_tokenless_observer_url(tmp_path) -> None:
    hub = ObserverHub(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0))
    try:
        session = hub.create_session("ask_text", "fake-model")
        raw_events = hub.get_events(session.session_id, raw=True)
        created = raw_events[0]["payload"]

        assert created["observer_url"].endswith(f"/sessions/{session.session_id}")
        assert "token=" not in created["observer_url"]
        assert created["observer_path"] == f"/sessions/{session.session_id}"
        assert "token=" in hub.observer_url(session.session_id)
    finally:
        hub.shutdown()


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

    assert "Previous session summary:" in prompt
    assert "Previous transcript JSON" not in prompt
    assert "Original prompt" in prompt
    assert "Original answer" in prompt
    assert "Final answer" in prompt
    assert "Follow up please" in prompt
    assert len(prompt) < 5000
