from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import GemnessConfig
from .models import ConversationRecord, Intervention, ObserverEvent, SessionRecord, SessionStatus, utc_now
from .redaction import redact_payload, redact_text


FINAL_STATUSES = {"valid", "invalid", "error", "cancelled", "completed"}
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


class SessionCancelled(RuntimeError):
    pass


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


class ObserverHub:
    def __init__(self, config: GemnessConfig) -> None:
        self.config = config
        self.transcript_dir = Path(config.transcript_dir)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        self._write_token_file()
        self._conversation_index_path = self.transcript_dir / "conversation-index.json"
        self.bus = EventBus()
        self.conversations: dict[str, ConversationRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.events: dict[str, list[ObserverEvent]] = {}
        self._loaded_event_ids: set[str] = set()
        self.interventions: dict[str, list[Intervention]] = {}
        self.pause_before_send = config.pause_before_send
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._web_server: Any = None
        self._web_server_error: str | None = None
        self.service: Any = None
        self._load_conversation_index()
        self._load_existing_events()

    def attach_service(self, service: Any) -> None:
        self.service = service

    def start_web_server(self) -> None:
        if not self.config.observer_enabled:
            return
        with self._lock:
            if self._web_server is not None:
                return
            from .web import ObserverWebServer

            try:
                self._web_server = ObserverWebServer(self, self.config.observer_host, self.config.observer_port)
            except OSError as exc:
                self._web_server_error = str(exc)
                return
            self._web_server_error = None
            self._web_server.start()

    @property
    def web_server_running(self) -> bool:
        with self._lock:
            return self._web_server is not None

    def shutdown(self) -> None:
        with self._lock:
            web_server = self._web_server
            self._web_server = None
        if web_server is not None:
            web_server.stop()

    @property
    def base_url(self) -> str:
        self.start_web_server()
        if self._web_server is None:
            if not self.config.observer_enabled or not self.config.observer_port:
                return ""
            return _observer_base_url(self.config.observer_host, self.config.observer_port)
        return self._web_server.base_url

    def observer_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/"

    def observer_public_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/"

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
        gemini_session_id: str | None = None,
        native_resume_enabled: bool = True,
        native_resume_used: bool = False,
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
            project_root=project_root,
            gemini_session_id=gemini_session_id,
            native_resume_enabled=native_resume_enabled,
            native_resume_used=native_resume_used,
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
                gemini_session_id=gemini_session_id,
                native_resume_enabled=native_resume_enabled,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                branch_from_conversation_id=branch_from_conversation_id,
                branch_from_run_id=branch_from_run_id,
                now=now,
            )
            session.conversation_id = conversation.conversation_id
            session.gemini_session_id = conversation.current_gemini_session_id
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
            self.interventions.setdefault(session.session_id, [])
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
                "project_root": project_root,
                "gemini_session_id": session.gemini_session_id,
                "native_resume_enabled": native_resume_enabled,
                "native_resume_used": native_resume_used,
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
        native_resume_used: bool | None = None,
        fallback_used: bool | None = None,
        fallback_reason: str | None = None,
        gemini_session_id: str | None = None,
        phase: str | None = None,
    ) -> None:
        with self._lock:
            session = self.sessions[session_id]
            session.command_argv = list(command_argv)
            if native_resume_used is not None:
                session.native_resume_used = native_resume_used
            if fallback_used is not None:
                session.fallback_used = fallback_used
            if fallback_reason is not None:
                session.fallback_reason = fallback_reason
            if gemini_session_id is not None:
                session.gemini_session_id = gemini_session_id
                if session.conversation_id and session.conversation_id in self.conversations:
                    self.conversations[session.conversation_id].current_gemini_session_id = gemini_session_id
            session.updated_at = utc_now()
            self._write_conversation_index()
        self.append_event(
            session_id,
            "run.command",
            "system",
            {
                "run_id": session_id,
                "command_argv": command_argv,
                "gemini_session_id": gemini_session_id,
                "native_resume_used": native_resume_used,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
            },
            phase=phase,
        )

    def rotate_gemini_session(self, session_id: str, gemini_session_id: str, reason: str) -> None:
        with self._lock:
            session = self.sessions[session_id]
            session.gemini_session_id = gemini_session_id
            session.fallback_used = True
            session.fallback_reason = reason
            if session.conversation_id and session.conversation_id in self.conversations:
                conversation = self.conversations[session.conversation_id]
                conversation.current_gemini_session_id = gemini_session_id
                conversation.fallback_mode = reason
                conversation.updated_at = utc_now()
            self._write_conversation_index()
        self.append_event(
            session_id,
            "conversation.native_session_rotated",
            "system",
            {"gemini_session_id": gemini_session_id, "reason": reason},
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
            self._cv.notify_all()
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
        self.append_event(
            session_id,
            "prompt.redacted",
            "system",
            {"prompt": redact_text(prompt)},
            redacted=True,
        )
        wait_for_approval = self.pause_before_send if force_approval is None else force_approval
        if wait_for_approval:
            self.set_status(
                session_id,
                "waiting_for_user_approval",
                "prompt.pending_approval",
                {"timeout_sec": self.config.approval_timeout_sec},
            )
            prompt = self._wait_for_prompt_approval(session_id, prompt)
        else:
            prompt = self._apply_available_pre_send_interventions(session_id, prompt)
        self.set_status(session_id, "sending")
        self.append_event(session_id, "prompt.sent", "codex_mcp", {"prompt": prompt})
        return prompt

    def add_intervention(
        self,
        session_id: str,
        action: str,
        *,
        instruction: str | None = None,
        prompt: str | None = None,
    ) -> Intervention:
        intervention = Intervention(
            intervention_id=str(uuid.uuid4()),
            session_id=session_id,
            action=action,
            instruction=instruction,
            prompt=prompt,
            ts=utc_now(),
        )
        with self._lock:
            if session_id not in self.sessions:
                raise KeyError(f"Unknown session_id: {session_id}")
            self.interventions.setdefault(session_id, []).append(intervention)
        self.append_event(session_id, "intervention.received", "user", intervention.to_dict())
        return intervention

    def pop_intervention(self, session_id: str, actions: Iterable[str] | None = None) -> Intervention | None:
        allowed = set(actions) if actions is not None else None
        with self._lock:
            items = self.interventions.setdefault(session_id, [])
            for index, item in enumerate(items):
                if item.status == "pending" and (allowed is None or item.action in allowed):
                    item.status = "applied"
                    items.pop(index)
                    self.append_event(session_id, "intervention.applied", "system", item.to_dict())
                    return item
        return None

    def consume_running_intervention(self, session_id: str) -> Intervention | None:
        while True:
            intervention = self.pop_intervention(session_id, {"cancel", "interrupt_retry", "note"})
            if intervention is None:
                return None
            if intervention.action == "note":
                continue
            return intervention

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
            events = self.get_events(run.session_id, raw=False)
            prompt = _last_event_text(events, {"prompt.sent", "prompt.rendered"}, "prompt")
            response = _last_final_result(events) or _last_gemini_response(events)
            if prompt or response:
                lines.append(f"Turn {run.turn_index or '?'}: User={_clip(prompt, 500)} | Gemini={_clip(response, 500)}")
        return "\n".join(lines) or "(none)"

    def build_follow_up_prompt(self, parent_session_id: str, instruction: str) -> str:
        self.refresh_from_disk()
        with self._lock:
            parent = self.sessions.get(parent_session_id)
            conversation_id = parent.conversation_id if parent else None
            summary = self.conversations.get(conversation_id).summary if conversation_id in self.conversations else None
            runs = [
                session
                for session in self.sessions.values()
                if session.session_id == parent_session_id
                or (conversation_id and session.conversation_id == conversation_id and (session.turn_index or 0) <= (parent.turn_index or 0))
            ]
            runs = sorted(runs, key=lambda item: (item.turn_index or 0, item.started_at))[-6:]
        recent_turns: list[str] = []
        for run in runs:
            events = self.get_events(run.session_id, raw=False)
            previous_prompt = _last_event_text(events, {"prompt.sent", "prompt.rendered"}, "prompt")
            previous_response = _last_gemini_response(events)
            final_result = _last_final_result(events)
            if previous_prompt:
                recent_turns.append(f"User: {_clip(previous_prompt, 1200)}")
            if previous_response:
                recent_turns.append(f"Gemini: {_clip(previous_response, 1200)}")
            if final_result and final_result != previous_response:
                recent_turns.append(f"Gemini final: {_clip(final_result, 1200)}")
        recent = "\n".join(recent_turns) or "(none)"
        return (
            "You are continuing a previous Gemini advisory conversation inside Gemness.\n\n"
            "Conversation summary:\n"
            f"{summary or '(none)'}\n\n"
            "Recent turns:\n"
            f"{recent}\n\n"
            "New user request:\n"
            f"{instruction}"
        )

    def validate_token(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.token)

    def refresh_from_disk(self) -> None:
        self._load_existing_events()

    def _wait_for_prompt_approval(self, session_id: str, prompt: str) -> str:
        deadline = time.monotonic() + self.config.approval_timeout_sec
        while True:
            prompt = self._apply_available_pre_send_interventions(session_id, prompt)
            intervention = self.pop_intervention(session_id, {"approve", "cancel"})
            if intervention is not None:
                if intervention.action == "approve":
                    return prompt
                self.set_status(session_id, "cancelled", "session.cancelled", {"reason": "user_cancelled"})
                raise SessionCancelled("Session cancelled before send")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.set_status(session_id, "error", "session.error", {"message": "Approval timed out"})
                raise SessionCancelled("Approval timed out")
            with self._cv:
                self._cv.wait(timeout=min(0.2, remaining))

    def _apply_available_pre_send_interventions(self, session_id: str, prompt: str) -> str:
        while True:
            intervention = self.pop_intervention(session_id, {"edit_prompt", "add_instruction", "cancel"})
            if intervention is None:
                return prompt
            if intervention.action == "cancel":
                self.set_status(session_id, "cancelled", "session.cancelled", {"reason": "user_cancelled"})
                raise SessionCancelled("Session cancelled before send")
            if intervention.action == "edit_prompt" and intervention.prompt is not None:
                prompt = intervention.prompt
                self.append_event(session_id, "prompt.rendered", "codex_mcp", {"prompt": prompt}, phase="edited")
                self.append_event(
                    session_id,
                    "prompt.redacted",
                    "system",
                    {"prompt": redact_text(prompt)},
                    phase="edited",
                    redacted=True,
                )
            elif intervention.action == "add_instruction" and intervention.instruction:
                prompt = f"{prompt}\n\nUser intervention:\n{intervention.instruction}"
                self.append_event(session_id, "prompt.rendered", "codex_mcp", {"prompt": prompt}, phase="appended_instruction")

    def _public_event(self, event: ObserverEvent, *, raw: bool) -> dict[str, Any]:
        data = event.to_dict()
        if not raw:
            data["payload"] = redact_payload(data.get("payload", {}))
            data["redacted"] = True
        return data

    def _session_dict(self, session: SessionRecord, *, raw: bool) -> dict[str, Any]:
        data = session.to_dict() | {"observer_url": self.observer_url(session.session_id)}
        if not raw and isinstance(data.get("title"), str):
            data["title"] = redact_text(data["title"])
        if not raw:
            data.pop("gemini_session_id", None)
            if "command_argv" in data:
                data["command_argv"] = redact_payload(data["command_argv"])
        return data

    def _conversation_dict(self, conversation: ConversationRecord, *, raw: bool) -> dict[str, Any]:
        data = conversation.to_dict()
        if not raw:
            data.pop("current_gemini_session_id", None)
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
        gemini_session_id: str | None,
        native_resume_enabled: bool,
        fallback_used: bool,
        fallback_reason: str | None,
        branch_from_conversation_id: str | None,
        branch_from_run_id: str | None,
        now: str,
    ) -> ConversationRecord:
        if conversation_id and conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            if gemini_session_id:
                conversation.current_gemini_session_id = gemini_session_id
            conversation.native_resume_enabled = native_resume_enabled
            return conversation
        new_conversation_id = conversation_id or _new_prefixed_id("conv")
        native_id = gemini_session_id or _new_prefixed_id("gemness")
        conversation = ConversationRecord(
            conversation_id=new_conversation_id,
            title=title,
            created_at=now,
            updated_at=now,
            project_root=project_root,
            model=model,
            approval_mode=self.config.gemini_approval_mode,
            current_gemini_session_id=native_id,
            native_resume_enabled=native_resume_enabled,
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
                    approval_mode=str(raw.get("approval_mode") or self.config.gemini_approval_mode),
                    current_gemini_session_id=str(raw["current_gemini_session_id"]),
                    native_resume_enabled=bool(raw.get("native_resume_enabled", True)),
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
            gemini_session_id = payload.get("gemini_session_id") if isinstance(payload.get("gemini_session_id"), str) else None
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
                    project_root=payload.get("project_root") if isinstance(payload.get("project_root"), str) else None,
                    gemini_session_id=gemini_session_id,
                    native_resume_enabled=bool(payload.get("native_resume_enabled", True)),
                    native_resume_used=bool(payload.get("native_resume_used", False)),
                    fallback_used=bool(payload.get("fallback_used", False)),
                    fallback_reason=payload.get("fallback_reason") if isinstance(payload.get("fallback_reason"), str) else None,
                    stream_events_path=payload.get("stream_events_path") if isinstance(payload.get("stream_events_path"), str) else None,
                ),
            )
            session = self.sessions[session_id]
            if conversation_id and conversation_id not in self.conversations and gemini_session_id:
                self.conversations[conversation_id] = ConversationRecord(
                    conversation_id=conversation_id,
                    title=session.title,
                    created_at=event.ts,
                    updated_at=event.ts,
                    project_root=session.project_root,
                    model=session.model,
                    approval_mode=str(payload.get("approval_mode") or self.config.gemini_approval_mode),
                    current_gemini_session_id=gemini_session_id,
                    native_resume_enabled=bool(payload.get("native_resume_enabled", True)),
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
            if "native_resume_used" in event.payload:
                session.native_resume_used = bool(event.payload.get("native_resume_used"))
            if "fallback_used" in event.payload:
                session.fallback_used = bool(event.payload.get("fallback_used"))
            if isinstance(event.payload.get("fallback_reason"), str):
                session.fallback_reason = event.payload["fallback_reason"]
            if isinstance(event.payload.get("gemini_session_id"), str):
                session.gemini_session_id = event.payload["gemini_session_id"]
        if event.type == "session.title":
            title = event.payload.get("title")
            if isinstance(title, str) and title.strip():
                session.title = title.strip()
                session.updated_at = event.ts
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


def _observer_base_url(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _new_prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def _result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    for key in ("text", "raw_response", "message"):
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


def _last_gemini_response(events: list[dict[str, Any]]) -> str:
    response = _last_event_text(events, {"gemini.response", "repair.response"}, "response")
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
        for key in ("text", "data", "raw_response", "message"):
            value = result.get(key)
            if value:
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ""


def _clip(value: str, limit: int = 2400) -> str:
    text = value.strip() if value else "(none)"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"
