from __future__ import annotations

import ctypes
import json
import os
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import GemnessConfig
from .mcp_metadata import SERVER_NAME, SERVER_VERSION
from .models import ConversationRecord, ObserverEvent, SessionRecord, SessionStatus, utc_now
from .observer_client import get_json, post_json
from .process_registry import (
    ProcessRecord,
    ProcessRegistry,
    REGISTRY_STALE_SEC,
    record_is_orphan,
    record_is_takeover_eligible,
    terminate_record_process,
    workspace_id_for_path,
)
from .redaction import redact_payload, redact_text


FINAL_STATUSES = {"valid", "invalid", "error", "cancelled", "completed"}
STALE_PROCESS_GRACE_SEC = 15
PUBLIC_TEXT_PREVIEW_CHARS = 4000
PUBLIC_COMMAND_ARG_PREVIEW_CHARS = 240
SESSION_STATUSES = {
    "queued",
    "waiting_for_user_approval",
    "sending",
    "running",
    "repairing",
    "valid",
    "invalid",
    "error",
    "cancelled",
    "completed",
}
OPEN_SESSION_STATUSES = SESSION_STATUSES - FINAL_STATUSES


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.RLock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class ObserverHub:
    def __init__(self, config: GemnessConfig) -> None:
        self.config = config
        self.transcript_dir = Path(config.transcript_dir)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        self._write_token_file()
        self._started_at_monotonic = time.monotonic()
        self._started_at_wall = time.time()
        self._conversation_index_path = self.transcript_dir / "conversation-index.json"
        self.bus = EventBus()
        self.conversations: dict[str, ConversationRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.events: dict[str, list[ObserverEvent]] = {}
        self._loaded_event_ids: set[str] = set()
        self._lock = threading.RLock()
        self._web_server: Any = None
        self._web_server_error: str | None = None
        self._observer_mode = "disabled" if not config.observer_enabled else "stopped"
        self._attached_base_url: str | None = None
        self._attached_owner_status: dict[str, Any] | None = None
        self._takeover_error: str | None = None
        self._ingest_error: str | None = None
        self._registry = ProcessRegistry(config)
        self._registry_stop = threading.Event()
        self._registry_thread: threading.Thread | None = None
        self.service: Any = None
        self._load_conversation_index()
        self._load_existing_events()
        self._write_registry_state()
        self._start_registry_heartbeat()

    def attach_service(self, service: Any) -> None:
        self.service = service

    def start_web_server(self) -> None:
        if not self.config.observer_enabled:
            return
        with self._lock:
            if self._web_server is not None:
                return
            if self._observer_mode in {"owner", "attached"}:
                return
            self._observer_mode = "starting"
        self._write_registry_state()
        if self._try_start_owner():
            return
        self._handle_bind_failure()

    def _try_start_owner(self) -> bool:
        if not self.config.observer_enabled:
            return False
        from .web import ObserverWebServer

        try:
            web_server = ObserverWebServer(self, self.config.observer_host, self.config.observer_port)
        except OSError as exc:
            with self._lock:
                self._web_server_error = str(exc)
            return False
        with self._lock:
            self._web_server = web_server
            self._web_server_error = None
            self._attached_base_url = None
            self._attached_owner_status = None
            self._takeover_error = None
            self._observer_mode = "owner"
        self._write_registry_state()
        web_server.start()
        return True

    def _handle_bind_failure(self) -> None:
        base_url = _observer_base_url(self.config.observer_host, self.config.observer_port)
        status, status_error = get_json(f"{base_url}/api/status")
        if _is_gemness_status(status):
            owner_record = self._owner_record_from_status(status)
            if owner_record is None or not owner_record.management_token:
                self._set_observer_mode(
                    "blocked",
                    owner_status=status,
                    observer_error="Gemness observer owner is reachable, but its registry token is unavailable.",
                )
                return
            if owner_record is not None and record_is_takeover_eligible(owner_record):
                self._set_observer_mode("takeover_pending", attached_to=base_url)
                if self._request_takeover(base_url, owner_record) and self._retry_start_owner():
                    return
            self._set_observer_mode("attached", attached_to=base_url, owner_status=status)
            return

        for owner_record in self._registry.observer_owner_records(self.config.observer_host, self.config.observer_port):
            if not record_is_takeover_eligible(owner_record):
                continue
            self._set_observer_mode("takeover_pending", attached_to=base_url)
            if owner_record.management_token and self._request_takeover(base_url, owner_record) and self._retry_start_owner():
                return
            if owner_record.pid == os.getpid():
                continue
            try:
                terminate_record_process(owner_record)
            except OSError as exc:
                self._takeover_error = str(exc)
            else:
                if self._retry_start_owner():
                    return

        message = status_error or self._web_server_error or "observer port is unavailable"
        self._set_observer_mode("blocked", observer_error=message)

    def _owner_record_from_status(self, status: dict[str, Any] | None) -> ProcessRecord | None:
        if not isinstance(status, dict):
            return None
        pid = status.get("pid")
        if not isinstance(pid, int):
            return None
        registry_id = status.get("registry_id")
        if isinstance(registry_id, str) and registry_id:
            for record in self._registry.list_records():
                if record.pid == pid and record.registry_id == registry_id:
                    return record
            return None
        return self._registry.read_pid(pid)

    def _request_takeover(self, base_url: str, owner_record: ProcessRecord) -> bool:
        token = owner_record.management_token
        if not token:
            self._takeover_error = "owner registry record has no management token"
            return False
        payload = {"requester_pid": os.getpid(), "requester_workspace_id": self.workspace_id, "reason": "stale_or_orphan_owner"}
        data, error, status_code = post_json(f"{base_url}/api/takeover", payload, token=token, timeout=1.0)
        if status_code and 200 <= status_code < 300 and data and data.get("accepted") is True:
            self._takeover_error = None
            return True
        if isinstance(data, dict) and data.get("error"):
            self._takeover_error = str(data["error"])
        else:
            self._takeover_error = str(error or status_code or "takeover rejected")
        return False

    def _retry_start_owner(self) -> bool:
        for _ in range(20):
            time.sleep(0.1)
            if self._try_start_owner():
                return True
        return False

    def _set_observer_mode(
        self,
        mode: str,
        *,
        attached_to: str | None = None,
        owner_status: dict[str, Any] | None = None,
        observer_error: str | None = None,
    ) -> None:
        with self._lock:
            self._observer_mode = mode
            self._attached_base_url = attached_to if mode in {"attached", "takeover_pending"} else None
            self._attached_owner_status = owner_status
            if observer_error is not None:
                self._web_server_error = observer_error
        self._write_registry_state()

    @property
    def web_server_running(self) -> bool:
        with self._lock:
            return self._web_server is not None

    def shutdown(self) -> None:
        self._registry_stop.set()
        registry_thread = self._registry_thread
        if registry_thread is not None:
            registry_thread.join(timeout=1)
        with self._lock:
            web_server = self._web_server
            self._web_server = None
            self._observer_mode = "stopped" if self.config.observer_enabled else "disabled"
        if web_server is not None:
            web_server.stop()
        self._registry.remove_current()

    @property
    def base_url(self) -> str:
        self.start_web_server()
        with self._lock:
            web_server = self._web_server
            attached_base_url = self._attached_base_url
        if web_server is None:
            if attached_base_url:
                return attached_base_url
            if not self.config.observer_enabled or not self.config.observer_port:
                return ""
            return _observer_base_url(self.config.observer_host, self.config.observer_port)
        return web_server.base_url

    def observer_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/"

    def observer_public_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/"

    @property
    def workspace_id(self) -> str:
        return workspace_id_for_path(self.config.workspace_root or Path.cwd())

    @property
    def observer_mode(self) -> str:
        with self._lock:
            return self._observer_mode

    def observer_state(self) -> dict[str, Any]:
        if self.config.observer_enabled:
            self.start_web_server()
        with self._lock:
            owner_status = self._attached_owner_status or {}
            owner_pid = os.getpid() if self._observer_mode == "owner" else owner_status.get("pid")
            attached_to = self._attached_base_url
            web_server = self._web_server
            mode = self._observer_mode
            bind_error = self._web_server_error
            takeover_error = self._takeover_error
            ingest_error = self._ingest_error
        return {
            "mode": mode,
            "owner_pid": owner_pid,
            "attached_to": attached_to,
            "workspace_id": self.workspace_id,
            "bind_error": bind_error,
            "takeover_error": takeover_error,
            "ingest_error": ingest_error,
            "running": web_server is not None,
            "url": self.base_url if self.config.observer_enabled else "",
        }

    def status_payload(self) -> dict[str, Any]:
        self.refresh_from_disk()
        with self._lock:
            active_run_count = sum(1 for session in self.sessions.values() if session.status in OPEN_SESSION_STATUSES)
            mode = self._observer_mode
            web_server = self._web_server
            observer_error = self._web_server_error
            attached_to = self._attached_base_url
        registry_record = self._registry.read_current()
        return {
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "pid": os.getpid(),
            "registry_id": registry_record.registry_id if registry_record is not None else None,
            "parent_pid": os.getppid(),
            "started_at": self._started_at_wall,
            "uptime_ms": int((time.monotonic() - self._started_at_monotonic) * 1000),
            "cwd": str(Path.cwd()),
            "workspace_id": self.workspace_id,
            "transcript_dir": str(self.transcript_dir),
            "observer": {
                "mode": mode,
                "host": self.config.observer_host,
                "port": self.config.observer_port,
                "url": self.base_url if self.config.observer_enabled else "",
                "owns_observer": web_server is not None,
                "attached_to": attached_to,
                "error": observer_error,
            },
            "known_workspaces": self.known_workspaces(),
            "active_run_count": active_run_count,
            "open_client_count": self.bus.subscriber_count,
            "takeover": self.takeover_eligibility(),
        }

    def known_workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            roots = sorted({session.project_root or "" for session in self.sessions.values()})
        if not roots:
            roots = [str(self.config.workspace_root or Path.cwd())]
        return [
            {
                "workspace_id": workspace_id_for_path(root or self.config.workspace_root or Path.cwd()),
                "cwd": root or str(self.config.workspace_root or Path.cwd()),
                "label": _workspace_label(root or str(self.config.workspace_root or Path.cwd())),
            }
            for root in roots
        ]

    def takeover_eligibility(self) -> dict[str, Any]:
        record = self._registry.read_current()
        reasons: list[str] = []
        if record is not None and record_is_orphan(record):
            reasons.append("parent_process_exited")
        if record is not None and record.last_seen_at < time.time() - REGISTRY_STALE_SEC:
            reasons.append("stale_registry_heartbeat")
        eligible = bool(reasons)
        return {"eligible": eligible, "reasons": reasons}

    def request_takeover(self, token: str | None) -> tuple[dict[str, Any], int]:
        if not self.validate_token(token):
            return {"accepted": False, "error": "invalid management token"}, 401
        eligibility = self.takeover_eligibility()
        if not eligibility["eligible"]:
            return {"accepted": False, "error": "observer owner is healthy", "takeover": eligibility}, 409
        self.bus.broadcast({"type": "observer.reconnect", "session_id": "", "payload": {"reason": "takeover"}})
        threading.Thread(target=self._shutdown_for_takeover, name="gemness-observer-takeover-shutdown", daemon=True).start()
        return {"accepted": True, "takeover": eligibility}, 202

    def ingest_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        event = ObserverEvent(
            event_id=str(event_data["event_id"]),
            session_id=str(event_data["session_id"]),
            parent_session_id=event_data.get("parent_session_id") if isinstance(event_data.get("parent_session_id"), str) else None,
            ts=str(event_data["ts"]),
            type=str(event_data["type"]),
            role=event_data["role"],
            tool_name=event_data.get("tool_name") if isinstance(event_data.get("tool_name"), str) else None,
            phase=event_data.get("phase") if isinstance(event_data.get("phase"), str) else None,
            payload=event_data.get("payload", {}) if isinstance(event_data.get("payload", {}), dict) else {},
            redacted=bool(event_data.get("redacted", False)),
        )
        with self._lock:
            if event.event_id in self._loaded_event_ids:
                return {"accepted": True, "duplicate": True}
            self._loaded_event_ids.add(event.event_id)
            self.events.setdefault(event.session_id, []).append(event)
            self._write_event(event)
            self._rebuild_session_from_event(event)
            self._write_conversation_index()
            public_event = self._public_event(event, raw=False)
        self.bus.broadcast(public_event)
        return {"accepted": True, "duplicate": False}

    def _shutdown_for_takeover(self) -> None:
        time.sleep(0.05)
        with self._lock:
            web_server = self._web_server
            self._web_server = None
            self._observer_mode = "takeover_pending"
        self._write_registry_state()
        if web_server is not None:
            web_server.stop()

    def _publish_attached_event(self, event: ObserverEvent) -> None:
        with self._lock:
            mode = self._observer_mode
            base_url = self._attached_base_url
            owner_status = self._attached_owner_status or {}
        if mode != "attached" or not base_url:
            return
        owner_record = self._owner_record_from_status(owner_status)
        token = owner_record.management_token if owner_record is not None else None
        if not token:
            with self._lock:
                self._ingest_error = "attached observer owner registry token is unavailable"
            self._recover_attached_owner(base_url, owner_record)
            return
        data, error, status_code = post_json(f"{base_url}/api/ingest", {"event": event.to_dict()}, token=token, timeout=1.0)
        with self._lock:
            if status_code and 200 <= status_code < 300 and data and data.get("accepted"):
                self._ingest_error = None
            else:
                self._ingest_error = str(data.get("error") if isinstance(data, dict) and data.get("error") else error or status_code or "ingest failed")
        if self._ingest_error:
            self._recover_attached_owner(base_url, owner_record)

    def _recover_attached_owner(self, base_url: str, owner_record: ProcessRecord | None) -> None:
        status, _ = get_json(f"{base_url}/api/status")
        if _is_gemness_status(status):
            refreshed_record = self._owner_record_from_status(status)
            if refreshed_record is None or not refreshed_record.management_token:
                self._set_observer_mode(
                    "blocked",
                    owner_status=status,
                    observer_error="Gemness observer owner is reachable, but its registry token is unavailable.",
                )
                return
            with self._lock:
                self._attached_owner_status = status
            return
        if owner_record is not None and not record_is_takeover_eligible(owner_record):
            return
        self._set_observer_mode("takeover_pending", attached_to=base_url)
        if owner_record is not None and owner_record.management_token and self._request_takeover(base_url, owner_record) and self._retry_start_owner():
            return
        if owner_record is not None and owner_record.pid != os.getpid():
            try:
                terminate_record_process(owner_record)
            except OSError as exc:
                self._takeover_error = str(exc)
            else:
                if self._retry_start_owner():
                    return
        if self._try_start_owner():
            return
        self._set_observer_mode("blocked", observer_error=self._web_server_error or self._ingest_error or "attached observer owner is unavailable")

    def _write_registry_state(self) -> None:
        with self._lock:
            mode = self._observer_mode
            owns_observer = self._web_server is not None
            observer_error = self._web_server_error
            attached_to = self._attached_base_url
        self._registry.write_current(
            observer_mode=mode,
            owns_observer=owns_observer,
            observer_error=observer_error,
            attached_to=attached_to,
            management_token=self.token,
        )

    def _start_registry_heartbeat(self) -> None:
        if self._registry_thread is not None:
            return

        def heartbeat() -> None:
            while not self._registry_stop.wait(5):
                self._write_registry_state()

        self._registry_thread = threading.Thread(target=heartbeat, name="gemness-process-registry", daemon=True)
        self._registry_thread.start()

    def create_session(
        self,
        tool_name: str,
        model: str,
        parent_session_id: str | None = None,
        title: str | None = None,
        *,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        branch_from_conversation_id: str | None = None,
        branch_from_run_id: str | None = None,
        project_root: str | None = None,
        agy_conversation_id: str | None = None,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ) -> SessionRecord:
        run_id = _new_prefixed_id("run")
        now = utc_now()
        session = SessionRecord(
            session_id=run_id,
            run_id=run_id,
            tool_name=tool_name,
            model=model,
            status="queued",
            started_at=now,
            parent_session_id=parent_session_id,
            title=title,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            branch_from_run_id=branch_from_run_id,
            workspace_id=self.workspace_id,
            project_root=project_root,
            agy_conversation_id=agy_conversation_id,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            stream_events_path=str(self.transcript_dir / f"{run_id}.jsonl"),
        )
        with self._lock:
            conversation = self._ensure_conversation_for_new_session(
                session,
                title=title,
                model=model,
                project_root=project_root,
                conversation_id=conversation_id,
                agy_conversation_id=agy_conversation_id,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                branch_from_conversation_id=branch_from_conversation_id,
                branch_from_run_id=branch_from_run_id,
                now=now,
            )
            session.conversation_id = conversation.conversation_id
            session.agy_conversation_id = conversation.current_agy_conversation_id
            session.turn_index = self._next_turn_index(conversation.conversation_id)
            if conversation.root_run_id is None:
                conversation.root_run_id = session.session_id
            conversation.turn_count = max(conversation.turn_count, session.turn_index)
            conversation.updated_at = now
            if project_root:
                conversation.project_root = project_root
            if title and not conversation.title:
                conversation.title = title
            if fallback_used:
                conversation.fallback_mode = fallback_reason or "fallback"
            self.sessions[session.session_id] = session
            self.events.setdefault(session.session_id, [])
            self._write_conversation_index()
        self.append_event(
            session.session_id,
            "session.created",
            "system",
            {
                "session_id": session.session_id,
                "run_id": session.run_id,
                "conversation_id": session.conversation_id,
                "parent_run_id": session.parent_run_id,
                "branch_from_conversation_id": branch_from_conversation_id,
                "branch_from_run_id": branch_from_run_id,
                "turn_index": session.turn_index,
                "tool_name": tool_name,
                "model": model,
                "status": "queued",
                "title": title,
                "workspace_id": self.workspace_id,
                "project_root": project_root,
                "agy_conversation_id": session.agy_conversation_id,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "stream_events_path": session.stream_events_path,
                "observer_url": self.observer_public_url(session.session_id),
                "observer_path": "/",
            },
            parent_session_id=parent_session_id,
            tool_name=tool_name,
        )
        return session

    def record_run_command(
        self,
        session_id: str,
        command_argv: list[str],
        *,
        fallback_used: bool | None = None,
        fallback_reason: str | None = None,
        agy_conversation_id: str | None = None,
        native_session_mode: str | None = None,
        model_requested: str | None = None,
        model_source: str | None = None,
        phase: str | None = None,
    ) -> None:
        safe_command_argv = _redact_prompt_argv(command_argv)
        with self._lock:
            session = self.sessions[session_id]
            session.command_argv = safe_command_argv
            if fallback_used is not None:
                session.fallback_used = fallback_used
            if fallback_reason is not None:
                session.fallback_reason = fallback_reason
            if agy_conversation_id is not None:
                session.agy_conversation_id = agy_conversation_id
                if session.conversation_id and session.conversation_id in self.conversations:
                    self.conversations[session.conversation_id].current_agy_conversation_id = agy_conversation_id
            session.updated_at = utc_now()
            self._write_conversation_index()
        self.append_event(
            session_id,
            "run.command",
            "system",
            {
                "run_id": session_id,
                "command_argv": safe_command_argv,
                "agy_conversation_id": agy_conversation_id,
                "native_session_mode": native_session_mode,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "model_requested": model_requested,
                "model_source": model_source,
            },
            phase=phase,
        )

    def set_model(self, session_id: str, model: str, *, source: str = "detected", phase: str | None = None) -> None:
        model = model.strip()
        if not model:
            return
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            changed = session.model != model
            session.model = model
            session.updated_at = utc_now()
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                conversation.model = model
                conversation.updated_at = session.updated_at
                self._write_conversation_index()
        self.append_event(session_id, "antigravity.model_detected", "gemness", {"model": model, "source": source, "changed": changed}, phase=phase)

    def set_agy_conversation_id(self, session_id: str, agy_conversation_id: str, *, source: str = "detected", phase: str | None = None) -> None:
        agy_conversation_id = agy_conversation_id.strip()
        if not agy_conversation_id:
            return
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            changed = session.agy_conversation_id != agy_conversation_id
            session.agy_conversation_id = agy_conversation_id
            session.updated_at = utc_now()
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                conversation.current_agy_conversation_id = agy_conversation_id
                conversation.updated_at = session.updated_at
                self._write_conversation_index()
        self.append_event(
            session_id,
            "conversation.agy_context_attached",
            "gemness",
            {"agy_conversation_id": agy_conversation_id, "source": source, "changed": changed},
            phase=phase,
        )

    def rotate_agy_conversation(self, session_id: str, agy_conversation_id: str, reason: str) -> None:
        with self._lock:
            session = self.sessions[session_id]
            session.agy_conversation_id = agy_conversation_id
            session.fallback_used = True
            session.fallback_reason = reason
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                conversation.current_agy_conversation_id = agy_conversation_id
                conversation.fallback_mode = reason
                conversation.updated_at = utc_now()
            self._write_conversation_index()
        self.append_event(
            session_id,
            "conversation.agy_context_rotated",
            "system",
            {"agy_conversation_id": agy_conversation_id, "reason": reason},
        )

    def append_event(
        self,
        session_id: str,
        event_type: str,
        role: str,
        payload: dict[str, Any] | None = None,
        *,
        parent_session_id: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        redacted: bool = False,
    ) -> ObserverEvent:
        payload = payload or {}
        with self._lock:
            session = self.sessions.get(session_id)
            if session is not None:
                parent_session_id = parent_session_id if parent_session_id is not None else session.parent_session_id
                tool_name = tool_name if tool_name is not None else session.tool_name
                session.updated_at = utc_now()
            event = ObserverEvent(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                parent_session_id=parent_session_id,
                ts=utc_now(),
                type=event_type,
                role=role,  # type: ignore[arg-type]
                tool_name=tool_name,
                phase=phase,
                payload=payload,
                redacted=redacted,
            )
            self.events.setdefault(session_id, []).append(event)
            self._loaded_event_ids.add(event.event_id)
            self._write_event(event)
            public_event = self._public_event(event, raw=False)
        self.bus.broadcast(public_event)
        self._publish_attached_event(event)
        return event

    def set_title(self, session_id: str, title: str | None) -> None:
        title = (title or "").strip()
        if not title:
            return
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None or session.title == title:
                return
            session.title = title
            session.updated_at = utc_now()
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                if not conversation.title:
                    conversation.title = title
                    conversation.updated_at = session.updated_at
                    self._write_conversation_index()
        self.append_event(session_id, "session.title", "system", {"title": title})

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        title = _validated_title(title)
        self.refresh_from_disk()
        with self._lock:
            session = self.sessions[session_id]
            if session.title == title:
                return self._session_dict(session, raw=False)
            session.title = title
            session.updated_at = utc_now()
        self.append_event(session_id, "session.title", "system", {"title": title})
        with self._lock:
            return self._session_dict(self.sessions[session_id], raw=False)

    def rename_conversation(self, conversation_id: str, title: str) -> dict[str, Any]:
        title = _validated_title(title)
        self.refresh_from_disk()
        root_session_id: str | None = None
        with self._lock:
            conversation = self.conversations[conversation_id]
            if conversation.title == title:
                return self._conversation_dict(conversation, raw=False)
            now = utc_now()
            conversation.title = title
            conversation.updated_at = now
            runs = [session for session in self.sessions.values() if session.conversation_id == conversation_id]
            root = _root_run(runs)
            if root is not None:
                root.title = title
                root.updated_at = now
                root_session_id = root.session_id
            self._write_conversation_index()
        if root_session_id is not None:
            self.append_event(root_session_id, "session.title", "system", {"title": title, "conversation_id": conversation_id})
        with self._lock:
            return self._conversation_dict(self.conversations[conversation_id], raw=False)

    def set_status(
        self,
        session_id: str,
        status: SessionStatus,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        *,
        role: str = "system",
        phase: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock:
            session = self.sessions[session_id]
            session.status = status
            session.updated_at = now
            if status in FINAL_STATUSES:
                session.completed_at = now
                session.duration_ms = _duration_ms(session.started_at, now)
                if payload and "result" in payload:
                    session.final_result = _result_text(payload.get("result"))
            if status == "valid":
                session.valid = True
            elif status in {"invalid", "error"}:
                session.valid = False
            if payload and "message" in payload:
                session.error = str(payload["message"])
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                conversation.updated_at = now
                if session.turn_index is not None:
                    conversation.turn_count = max(conversation.turn_count, session.turn_index)
                if session.fallback_used and session.fallback_reason:
                    conversation.fallback_mode = session.fallback_reason
                self._write_conversation_index()
        if event_type:
            event_payload = {"status": status}
            if payload:
                event_payload.update(payload)
            self.append_event(session_id, event_type, role, event_payload, phase=phase)

    def prepare_prompt(self, session_id: str, prompt: str, *, force_approval: bool | None = None) -> str:
        self.append_event(session_id, "prompt.rendered", "codex_mcp", {"prompt": prompt})
        self.set_status(session_id, "sending")
        self.append_event(
            session_id,
            "prompt.sent",
            "codex_mcp",
            {
                "prompt_ref": "prompt.rendered",
                "prompt_preview": _clip_with_marker(redact_text(prompt), PUBLIC_TEXT_PREVIEW_CHARS),
                "prompt_chars": len(prompt),
                "force_approval": bool(force_approval),
            },
        )
        return prompt

    def list_sessions(self) -> list[dict[str, Any]]:
        self.refresh_from_disk()
        with self._lock:
            sessions = [self._session_dict(session, raw=False) for session in self.sessions.values()]
        return sorted(sessions, key=lambda item: item["started_at"], reverse=True)

    def list_conversations(self) -> list[dict[str, Any]]:
        self.refresh_from_disk()
        with self._lock:
            conversations = [self._conversation_dict(conversation, raw=False) for conversation in self.conversations.values()]
        return sorted(conversations, key=lambda item: item["updated_at"], reverse=True)

    def get_session(self, session_id: str, *, raw: bool = True) -> dict[str, Any]:
        self.refresh_from_disk()
        with self._lock:
            return self._session_dict(self.sessions[session_id], raw=raw)

    def get_conversation(self, conversation_id: str, *, raw: bool = True) -> dict[str, Any]:
        self.refresh_from_disk()
        with self._lock:
            return self._conversation_dict(self.conversations[conversation_id], raw=raw)

    def get_events(self, session_id: str, *, raw: bool = False) -> list[dict[str, Any]]:
        self.refresh_from_disk()
        with self._lock:
            events = list(self.events.get(session_id, []))
        return [self._public_event(event, raw=raw) for event in events]

    def export_transcript(self, session_id: str, *, raw: bool = False) -> dict[str, Any]:
        return {
            "session": self.get_session(session_id, raw=raw),
            "events": self.get_events(session_id, raw=raw),
            "raw": raw,
        }

    def export_conversation(self, conversation_id: str, *, raw: bool = False) -> dict[str, Any]:
        self.refresh_from_disk()
        with self._lock:
            conversation = self.conversations[conversation_id]
            runs = sorted(
                [session for session in self.sessions.values() if session.conversation_id == conversation_id],
                key=lambda item: (item.turn_index or 0, item.started_at),
            )
        events: list[dict[str, Any]] = []
        for run in runs:
            events.extend(self.get_events(run.session_id, raw=raw))
        return {
            "conversation": self._conversation_dict(conversation, raw=raw),
            "runs": [self._session_dict(run, raw=raw) for run in runs],
            "events": events,
            "raw": raw,
        }

    def write_text_artifact(self, session_id: str, name: str, content: str) -> dict[str, Any]:
        safe_name = _safe_artifact_name(name)
        path = self.transcript_dir / f"{session_id}.{safe_name}"
        path.write_text(content, encoding="utf-8")
        return {
            "kind": "text",
            "name": safe_name,
            "path": str(path),
            "bytes": len(content.encode("utf-8", errors="replace")),
            "encoding": "utf-8",
        }

    def conversation_runs(self, conversation_id: str) -> list[SessionRecord]:
        self.refresh_from_disk()
        with self._lock:
            return sorted(
                [session for session in self.sessions.values() if session.conversation_id == conversation_id],
                key=lambda item: (item.turn_index or 0, item.started_at),
            )

    def is_latest_run(self, session_id: str) -> bool:
        self.refresh_from_disk()
        with self._lock:
            session = self.sessions[session_id]
            if not session.conversation_id:
                return True
            siblings = [item for item in self.sessions.values() if item.conversation_id == session.conversation_id]
            if not siblings:
                return True
            latest = max(siblings, key=lambda item: (item.turn_index or 0, item.started_at))
            return latest.session_id == session_id

    def root_run_id(self, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        self.refresh_from_disk()
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if conversation and conversation.root_run_id:
                return conversation.root_run_id
            runs = [session for session in self.sessions.values() if session.conversation_id == conversation_id]
            if not runs:
                return None
            root = min(runs, key=lambda item: (item.turn_index or 0, item.started_at))
            return root.session_id

    def update_conversation_summary(self, conversation_id: str, summary: str) -> None:
        with self._lock:
            conversation = self.conversations[conversation_id]
            conversation.summary = summary
            conversation.updated_at = utc_now()
            self._write_conversation_index()

    def summarize_conversation(self, conversation_id: str, *, through_run_id: str | None = None) -> str:
        runs = self.conversation_runs(conversation_id)
        if through_run_id:
            through = next((run for run in runs if run.session_id == through_run_id), None)
            if through is not None:
                runs = [run for run in runs if (run.turn_index or 0) <= (through.turn_index or 0)]
        lines: list[str] = []
        for run in runs[-8:]:
            title = _clip_single_line(run.title or run.tool_name or "untitled", 120)
            lines.append(f"Turn {run.turn_index or '?'}: tool={run.tool_name}, status={run.status}, title={title}")
        return "\n".join(lines) or "(none)"

    def build_follow_up_prompt(self, parent_session_id: str, instruction: str) -> str:
        self.refresh_from_disk()
        with self._lock:
            parent = self.sessions.get(parent_session_id)
            conversation_id = parent.conversation_id if parent else None
            summary = self.conversations.get(conversation_id).summary if conversation_id in self.conversations else None
        summary_text = _clip(summary, 1200) if summary else "(none)"
        if summary:
            return f"Context summary:\n{summary_text}\n\nUser follow-up:\n{instruction}"
        return f"User follow-up:\n{instruction}"

    def validate_token(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.token)

    def refresh_from_disk(self) -> None:
        self._load_existing_events()
        self._settle_stale_sessions()

    def _settle_stale_sessions(self) -> None:
        stale: list[tuple[str, str]] = []
        now = time.time()
        with self._lock:
            for session in self.sessions.values():
                if session.status not in OPEN_SESSION_STATUSES:
                    continue
                age = _age_seconds(session.updated_at, now)
                pid = _last_started_pid(self.events.get(session.session_id, []))
                reason = ""
                if session.status == "queued" and _is_managed_run(self.service, session.session_id):
                    continue
                if pid is not None and age > STALE_PROCESS_GRACE_SEC and not _process_is_running(pid):
                    reason = f"process {pid} is no longer running"
                elif age > self.config.agy_timeout_sec + STALE_PROCESS_GRACE_SEC:
                    reason = f"no observer updates for {int(age)} seconds"
                if reason:
                    stale.append((session.session_id, reason))
        for session_id, reason in stale:
            with self._lock:
                session = self.sessions.get(session_id)
                if session is None or session.status not in OPEN_SESSION_STATUSES:
                    continue
            self.set_status(
                session_id,
                "error",
                "session.error",
                {"message": f"Stale observer session marked as error: {reason}", "reason": "stale_observer_session"},
            )

    def _public_event(self, event: ObserverEvent, *, raw: bool) -> dict[str, Any]:
        data = event.to_dict()
        if not raw:
            data["payload"] = _compact_public_payload(event.type, redact_payload(data.get("payload", {})))
            data["redacted"] = True
        return data

    def _session_dict(self, session: SessionRecord, *, raw: bool) -> dict[str, Any]:
        data = session.to_dict() | {"observer_url": self.observer_url(session.session_id)}
        data.setdefault("workspace_id", workspace_id_for_path(session.project_root or self.config.workspace_root or Path.cwd()))
        data.setdefault("workspace_label", _workspace_label(session.project_root or str(self.config.workspace_root or Path.cwd())))
        if session.conversation_id and session.conversation_id in self.conversations:
            conversation_title = self.conversations[session.conversation_id].title
            if conversation_title:
                data["conversation_title"] = conversation_title
        if not raw and isinstance(data.get("title"), str):
            data["title"] = redact_text(data["title"])
        if not raw:
            if isinstance(data.get("conversation_title"), str):
                data["conversation_title"] = redact_text(data["conversation_title"])
            data.pop("agy_conversation_id", None)
            if "command_argv" in data:
                data["command_argv"] = _compact_command_argv(redact_payload(data["command_argv"]))
        return data

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        self.refresh_from_disk()
        paths: list[Path] = []
        deleted_event_ids: set[str] = set()
        with self._lock:
            conversation = self.conversations[conversation_id]
            runs = [session for session in self.sessions.values() if session.conversation_id == conversation_id]
            active = [session.session_id for session in runs if session.status not in FINAL_STATUSES]
            if active:
                raise ValueError("running conversation cannot be removed")
            for session in runs:
                paths.extend(self._event_paths_for_session(session))
                deleted_event_ids.update(event.event_id for event in self.events.get(session.session_id, []))
            for session in runs:
                self.sessions.pop(session.session_id, None)
                self.events.pop(session.session_id, None)
            self._loaded_event_ids.difference_update(deleted_event_ids)
            self.conversations.pop(conversation.conversation_id, None)
            self._write_conversation_index()
        _unlink_paths(paths)
        return {"conversation_id": conversation_id, "deleted_runs": len(runs)}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        self.refresh_from_disk()
        paths: list[Path] = []
        with self._lock:
            session = self.sessions[session_id]
            if session.status not in FINAL_STATUSES:
                raise ValueError("running session cannot be removed")
            if session.conversation_id:
                conversation_id = session.conversation_id
            else:
                conversation_id = None
                paths.extend(self._event_paths_for_session(session))
                self._loaded_event_ids.difference_update(event.event_id for event in self.events.get(session_id, []))
                self.sessions.pop(session_id, None)
                self.events.pop(session_id, None)
        if conversation_id:
            result = self.delete_conversation(conversation_id)
            return {"session_id": session_id, **result}
        _unlink_paths(paths)
        return {"session_id": session_id, "deleted_runs": 1}

    def _event_paths_for_session(self, session: SessionRecord) -> list[Path]:
        root = self.transcript_dir.resolve()
        candidates = [self.transcript_dir / f"{session.session_id}.jsonl"]
        if session.stream_events_path:
            candidates.append(Path(session.stream_events_path))
        candidates.extend(self.transcript_dir.glob(f"{session.session_id}.*"))
        paths: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if not resolved.is_relative_to(root) or resolved in seen:
                continue
            seen.add(resolved)
            paths.append(resolved)
        return paths

    def _conversation_dict(self, conversation: ConversationRecord, *, raw: bool) -> dict[str, Any]:
        data = conversation.to_dict()
        if not raw:
            data.pop("current_agy_conversation_id", None)
            if isinstance(data.get("title"), str):
                data["title"] = redact_text(data["title"])
            if isinstance(data.get("summary"), str):
                data["summary"] = redact_text(data["summary"])
        return data

    def _ensure_conversation_for_new_session(
        self,
        session: SessionRecord,
        *,
        title: str | None,
        model: str,
        project_root: str | None,
        conversation_id: str | None,
        agy_conversation_id: str | None,
        fallback_used: bool,
        fallback_reason: str | None,
        branch_from_conversation_id: str | None,
        branch_from_run_id: str | None,
        now: str,
    ) -> ConversationRecord:
        if conversation_id and conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            if agy_conversation_id:
                conversation.current_agy_conversation_id = agy_conversation_id
            return conversation
        new_conversation_id = conversation_id or _new_prefixed_id("conv")
        agy_id = agy_conversation_id or _new_prefixed_id("gemness")
        conversation = ConversationRecord(
            conversation_id=new_conversation_id,
            title=title,
            created_at=now,
            updated_at=now,
            project_root=project_root,
            model=model,
            approval_mode="antigravity_cli_settings",
            current_agy_conversation_id=agy_id,
            fallback_mode=fallback_reason if fallback_used and fallback_reason else "none",
            root_run_id=session.session_id,
            branch_from_conversation_id=branch_from_conversation_id,
            branch_from_run_id=branch_from_run_id,
        )
        self.conversations[new_conversation_id] = conversation
        return conversation

    def _next_turn_index(self, conversation_id: str) -> int:
        indexes = [
            session.turn_index or 0
            for session in self.sessions.values()
            if session.conversation_id == conversation_id
        ]
        return (max(indexes) if indexes else 0) + 1

    def _write_conversation_index(self) -> None:
        payload = {
            "conversations": [
                conversation.to_dict()
                for conversation in sorted(self.conversations.values(), key=lambda item: item.created_at)
            ]
        }
        tmp_path = self._conversation_index_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._conversation_index_path)

    def _load_conversation_index(self) -> None:
        if not self._conversation_index_path.exists():
            return
        try:
            payload = json.loads(self._conversation_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in payload.get("conversations", []):
            if not isinstance(raw, dict):
                continue
            try:
                conversation = ConversationRecord(
                    conversation_id=str(raw["conversation_id"]),
                    title=raw.get("title") if isinstance(raw.get("title"), str) else None,
                    created_at=str(raw["created_at"]),
                    updated_at=str(raw.get("updated_at") or raw["created_at"]),
                    project_root=raw.get("project_root") if isinstance(raw.get("project_root"), str) else None,
                    model=str(raw.get("model") or ""),
                    approval_mode=str(raw.get("approval_mode") or "antigravity_cli_settings"),
                    current_agy_conversation_id=str(raw["current_agy_conversation_id"]),
                    fallback_mode=str(raw.get("fallback_mode") or "none"),
                    summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
                    turn_count=int(raw.get("turn_count") or 0),
                    root_run_id=raw.get("root_run_id") if isinstance(raw.get("root_run_id"), str) else None,
                    branch_from_conversation_id=raw.get("branch_from_conversation_id")
                    if isinstance(raw.get("branch_from_conversation_id"), str)
                    else None,
                    branch_from_run_id=raw.get("branch_from_run_id") if isinstance(raw.get("branch_from_run_id"), str) else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.conversations[conversation.conversation_id] = conversation

    def _write_event(self, event: ObserverEvent) -> None:
        path = self.transcript_dir / f"{event.session_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _write_token_file(self) -> None:
        token_path = self.transcript_dir / "observer-token.txt"
        token_path.write_text(self.token + "\n", encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass

    def _load_existing_events(self) -> None:
        for path in self.transcript_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    event = ObserverEvent(
                        event_id=raw["event_id"],
                        session_id=raw["session_id"],
                        parent_session_id=raw.get("parent_session_id"),
                        ts=raw["ts"],
                        type=raw["type"],
                        role=raw["role"],
                        tool_name=raw.get("tool_name"),
                        phase=raw.get("phase"),
                        payload=raw.get("payload", {}),
                        redacted=bool(raw.get("redacted", False)),
                    )
                    with self._lock:
                        if event.event_id in self._loaded_event_ids:
                            continue
                        self._loaded_event_ids.add(event.event_id)
                        self.events.setdefault(event.session_id, []).append(event)
                        self._rebuild_session_from_event(event)
            except (OSError, KeyError, json.JSONDecodeError):
                continue

    def _rebuild_session_from_event(self, event: ObserverEvent) -> None:
        if event.type == "session.created":
            payload = event.payload
            session_id = event.session_id
            conversation_id = payload.get("conversation_id") if isinstance(payload.get("conversation_id"), str) else None
            agy_conversation_id = payload.get("agy_conversation_id") if isinstance(payload.get("agy_conversation_id"), str) else None
            self.sessions.setdefault(
                session_id,
                SessionRecord(
                    session_id=session_id,
                    run_id=str(payload.get("run_id") or session_id),
                    tool_name=str(payload.get("tool_name") or event.tool_name or "unknown"),
                    model=str(payload.get("model") or ""),
                    status=payload.get("status", "queued"),  # type: ignore[arg-type]
                    started_at=event.ts,
                    parent_session_id=event.parent_session_id,
                    title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                    conversation_id=conversation_id,
                    parent_run_id=payload.get("parent_run_id") if isinstance(payload.get("parent_run_id"), str) else None,
                    branch_from_run_id=payload.get("branch_from_run_id") if isinstance(payload.get("branch_from_run_id"), str) else None,
                    turn_index=payload.get("turn_index") if isinstance(payload.get("turn_index"), int) else None,
                    workspace_id=payload.get("workspace_id") if isinstance(payload.get("workspace_id"), str) else None,
                    project_root=payload.get("project_root") if isinstance(payload.get("project_root"), str) else None,
                    agy_conversation_id=agy_conversation_id,
                    fallback_used=bool(payload.get("fallback_used", False)),
                    fallback_reason=payload.get("fallback_reason") if isinstance(payload.get("fallback_reason"), str) else None,
                    stream_events_path=payload.get("stream_events_path") if isinstance(payload.get("stream_events_path"), str) else None,
                ),
            )
            session = self.sessions[session_id]
            if conversation_id and conversation_id not in self.conversations and agy_conversation_id:
                self.conversations[conversation_id] = ConversationRecord(
                    conversation_id=conversation_id,
                    title=session.title,
                    created_at=event.ts,
                    updated_at=event.ts,
                    project_root=session.project_root,
                    model=session.model,
                    approval_mode=str(payload.get("approval_mode") or "antigravity_cli_settings"),
                    current_agy_conversation_id=agy_conversation_id,
                    fallback_mode=session.fallback_reason if session.fallback_used and session.fallback_reason else "none",
                    turn_count=session.turn_index or 0,
                    root_run_id=session.session_id if not event.parent_session_id else None,
                    branch_from_conversation_id=payload.get("branch_from_conversation_id")
                    if isinstance(payload.get("branch_from_conversation_id"), str)
                    else None,
                    branch_from_run_id=session.branch_from_run_id,
                )
        session = self.sessions.get(event.session_id)
        if session is None:
            return
        if event.type == "run.command":
            argv = event.payload.get("command_argv")
            if isinstance(argv, list):
                session.command_argv = [str(item) for item in argv]
            if "fallback_used" in event.payload:
                session.fallback_used = bool(event.payload.get("fallback_used"))
            if isinstance(event.payload.get("fallback_reason"), str):
                session.fallback_reason = event.payload["fallback_reason"]
            if isinstance(event.payload.get("agy_conversation_id"), str):
                session.agy_conversation_id = event.payload["agy_conversation_id"]
        if event.type == "conversation.agy_context_attached":
            agy_conversation_id = event.payload.get("agy_conversation_id")
            if isinstance(agy_conversation_id, str) and agy_conversation_id.strip():
                session.agy_conversation_id = agy_conversation_id.strip()
        if event.type == "antigravity.model_detected":
            model = event.payload.get("model")
            if isinstance(model, str) and model.strip():
                session.model = model.strip()
        if event.type == "session.title":
            title = event.payload.get("title")
            if isinstance(title, str) and title.strip():
                session.title = title.strip()
                session.updated_at = event.ts
                conversation_id = event.payload.get("conversation_id") if isinstance(event.payload.get("conversation_id"), str) else session.conversation_id
                if conversation_id and conversation_id in self.conversations:
                    conversation = self.conversations[conversation_id]
                    if event.payload.get("conversation_id") == conversation_id or conversation.root_run_id == session.session_id:
                        conversation.title = session.title
                        conversation.updated_at = max(conversation.updated_at, event.ts)
        status = event.payload.get("status")
        if isinstance(status, str) and status in SESSION_STATUSES:
            session.status = status  # type: ignore[assignment]
            session.updated_at = event.ts
            if status in FINAL_STATUSES:
                session.completed_at = event.ts
                session.duration_ms = _duration_ms(session.started_at, event.ts)
                if "result" in event.payload:
                    session.final_result = _result_text(event.payload.get("result"))
        if session.conversation_id and session.conversation_id in self.conversations:
            conversation = self.conversations[session.conversation_id]
            conversation.updated_at = max(conversation.updated_at, session.updated_at)
            if session.agy_conversation_id:
                conversation.current_agy_conversation_id = session.agy_conversation_id
            if session.turn_index is not None:
                conversation.turn_count = max(conversation.turn_count, session.turn_index)
            if conversation.root_run_id is None or session.turn_index == 1:
                conversation.root_run_id = session.session_id


def _duration_ms(started_at: str, completed_at: str) -> int:
    try:
        start = _parse_iso(started_at)
        end = _parse_iso(completed_at)
        return max(0, int((end - start) * 1000))
    except ValueError:
        return 0


def _parse_iso(value: str) -> float:
    parsed = value.replace("Z", "+00:00")
    from datetime import datetime

    return datetime.fromisoformat(parsed).timestamp()


def _age_seconds(updated_at: str, now: float) -> float:
    try:
        return max(0.0, now - _parse_iso(updated_at))
    except ValueError:
        return 0.0


def _last_started_pid(events: list[ObserverEvent]) -> int | None:
    for event in reversed(events):
        if event.type != "antigravity.started":
            continue
        pid = event.payload.get("pid")
        if isinstance(pid, int) and pid > 0:
            return pid
    return None


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_is_running(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _observer_base_url(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _is_gemness_status(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    server = status.get("server")
    return isinstance(server, dict) and server.get("name") == SERVER_NAME


def _workspace_label(path: str | Path | None) -> str:
    if path is None:
        return "workspace"
    text = str(path).rstrip("\\/")
    if not text:
        return "workspace"
    name = Path(text).name
    return name or text


def _is_managed_run(service: Any, session_id: str) -> bool:
    run_manager = getattr(service, "run_manager", None)
    is_managed = getattr(run_manager, "is_managed", None)
    return bool(callable(is_managed) and is_managed(session_id))


def _validated_title(title: str) -> str:
    cleaned = " ".join(str(title or "").split())
    if not cleaned:
        raise ValueError("title is required")
    if len(cleaned) > 120:
        raise ValueError("title must be 120 characters or fewer")
    return cleaned


def _root_run(runs: list[SessionRecord]) -> SessionRecord | None:
    if not runs:
        return None
    return min(runs, key=lambda item: (item.turn_index or 0, item.started_at))


def _safe_artifact_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name.strip())
    return cleaned or "artifact.txt"


def _compact_public_payload(event_type: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return _compact_public_value(payload)
    compacted = {key: _compact_public_value(value) for key, value in payload.items()}
    if "result" in compacted:
        compacted["result"] = _compact_public_value(compacted["result"])
    if "stdout_artifact" in compacted:
        compacted["stdout_artifact"] = _public_artifact_ref(compacted["stdout_artifact"])
    if event_type == "antigravity.response" and isinstance(payload.get("response"), str):
        compacted["response"] = _compact_response_envelope(payload["response"])
    compacted = _compact_command_fields(compacted)
    return compacted


def _compact_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clip_with_marker(value, PUBLIC_TEXT_PREVIEW_CHARS)
    if isinstance(value, list):
        return [_compact_public_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_compact_public_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _compact_public_value(item) for key, item in value.items()}
    return value


def _compact_command_argv(argv: Any) -> Any:
    if not isinstance(argv, list):
        return _compact_public_value(argv)
    return [_clip_with_marker(str(item), PUBLIC_COMMAND_ARG_PREVIEW_CHARS) for item in _redact_prompt_argv(argv)]


def _compact_command_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_command_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    compacted: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"command", "command_argv"} and isinstance(item, list):
            compacted[key] = _compact_command_argv(item)
        else:
            compacted[key] = _compact_command_fields(item)
    return compacted


def _public_artifact_ref(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key in {"kind", "name", "bytes", "encoding"}}


def _redact_prompt_argv(argv: Any) -> Any:
    if not isinstance(argv, list):
        return argv
    redacted: list[str] = []
    redact_next = False
    for item in argv:
        value = str(item)
        if redact_next:
            redacted.append("[PROMPT_REDACTED]")
            redact_next = False
            continue
        redacted.append(value)
        if value in {"-p", "--print", "--prompt"}:
            redact_next = True
    return redacted


def _compact_response_envelope(value: str) -> str:
    try:
        envelope = json.loads(value)
    except json.JSONDecodeError:
        return _clip_with_marker(value, PUBLIC_TEXT_PREVIEW_CHARS)
    if not isinstance(envelope, dict):
        return _clip_with_marker(value, PUBLIC_TEXT_PREVIEW_CHARS)
    compacted = _compact_command_fields(_compact_public_value(envelope))
    return json.dumps(compacted, ensure_ascii=False)


def _clip_with_marker(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n...[truncated {len(value) - limit} chars]"


def _unlink_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _new_prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def _result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    for key in ("text", "response_preview", "raw_response", "message"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    data = result.get("data")
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return ""


def _last_event_text(events: list[dict[str, Any]], event_types: set[str], payload_key: str) -> str:
    for event in reversed(events):
        if event.get("type") in event_types:
            value = event.get("payload", {}).get(payload_key)
            if isinstance(value, str):
                return value
    return ""


def _last_antigravity_response(events: list[dict[str, Any]]) -> str:
    response = _last_event_text(events, {"antigravity.response", "repair.response"}, "response")
    if not response:
        response = _last_event_text(events, {"antigravity.response", "repair.response"}, "response_preview")
    if not response:
        return ""
    try:
        envelope = json.loads(response)
    except json.JSONDecodeError:
        return response
    if isinstance(envelope, dict) and isinstance(envelope.get("response"), str):
        return envelope["response"]
    return response


def _last_final_result(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") != "session.completed":
            continue
        result = event.get("payload", {}).get("result")
        if not isinstance(result, dict):
            continue
        for key in ("text", "data", "response_preview", "raw_response", "message"):
            value = result.get(key)
            if value:
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ""


def _clip(value: str, limit: int = 2400) -> str:
    text = value.strip() if value else "(none)"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _clip_single_line(value: str, limit: int) -> str:
    text = " ".join((value or "(none)").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
