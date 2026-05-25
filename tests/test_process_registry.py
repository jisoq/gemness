from __future__ import annotations

import json
import os
import time

from gemness.config import GemnessConfig
from gemness.process_registry import ProcessRecord, ProcessRegistry


def test_process_registry_writes_public_status_without_token(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path / "transcripts", process_registry_dir=tmp_path / "processes", observer_enabled=False)
    registry = ProcessRegistry(config)

    record = registry.write_current(observer_mode="owner", owns_observer=True, management_token="secret-token")

    loaded = registry.read_pid(os.getpid())
    status = registry.status()

    assert loaded is not None
    assert loaded.management_token == "secret-token"
    assert loaded.registry_id == record.registry_id
    assert status["records"][0]["has_management_token"] is True
    assert "management_token" not in status["records"][0]


def test_process_registry_cleanup_removes_dead_records(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path / "transcripts", process_registry_dir=tmp_path / "processes", observer_enabled=False)
    registry = ProcessRegistry(config)
    dead = ProcessRecord(
        pid=999999,
        parent_pid=None,
        started_at=time.time() - 60,
        last_seen_at=time.time() - 60,
        argv=["gemness"],
        cwd=str(tmp_path),
        workspace_id="ws_dead",
        transcript_dir=str(tmp_path / "transcripts"),
        observer_host="127.0.0.1",
        observer_port=56755,
        observer_mode="owner",
        owns_observer=True,
        version="test",
        registry_id="dead",
    )
    (config.process_registry_dir / "999999-dead.json").write_text(json.dumps(dead.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = registry.cleanup(stale=True)

    assert result["removed"] == [999999]
    assert not (config.process_registry_dir / "999999-dead.json").exists()


def test_process_registry_terminate_orphans_only_for_registered_orphans(tmp_path, monkeypatch) -> None:
    config = GemnessConfig(transcript_dir=tmp_path / "transcripts", process_registry_dir=tmp_path / "processes", observer_enabled=False)
    registry = ProcessRegistry(config)
    orphan = ProcessRecord(
        pid=12345,
        parent_pid=54321,
        started_at=time.time(),
        last_seen_at=time.time(),
        argv=["gemness"],
        cwd=str(tmp_path),
        workspace_id="ws_orphan",
        transcript_dir=str(tmp_path / "transcripts"),
        observer_host="127.0.0.1",
        observer_port=56755,
        observer_mode="owner",
        owns_observer=True,
        version="test",
        registry_id="orphan",
    )
    (config.process_registry_dir / "12345-orphan.json").write_text(json.dumps(orphan.to_dict(), ensure_ascii=False), encoding="utf-8")
    terminated: list[int] = []

    monkeypatch.setattr("gemness.process_registry.process_is_running", lambda pid: pid == 12345)
    monkeypatch.setattr("gemness.process_registry.terminate_process", lambda pid: terminated.append(pid))

    result = registry.cleanup(stale=False, terminate_orphans=True)

    assert terminated == [12345]
    assert result["terminated"] == [12345]
    assert not (config.process_registry_dir / "12345-orphan.json").exists()
