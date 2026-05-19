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
from .models import Intervention, ObserverEvent, SessionRecord, SessionStatus, utc_now
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
        self.bus = EventBus()
        self.sessions: dict[str, SessionRecord] = {}
        self.events: dict[str, list[ObserverEvent]] = {}
        self.interventions: dict[str, list[Intervention]] = {}
        self.pause_before_send = config.pause_before_send
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._web_server: Any = None
        self.service: Any = None
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

            self._web_server = ObserverWebServer(self, self.config.observer_host, self.config.observer_port)
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
            return ""
        return self._web_server.base_url

    def observer_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/sessions/{session_id}?token={self.token}"

    def observer_public_url(self, session_id: str) -> str:
        if not self.config.observer_enabled:
            return ""
        return f"{self.base_url}/sessions/{session_id}"

    def create_session(self, tool_name: str, model: str, parent_session_id: str | None = None) -> SessionRecord:
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            tool_name=tool_name,
            model=model,
            status="queued",
            started_at=utc_now(),
            parent_session_id=parent_session_id,
        )
        with self._lock:
            self.sessions[session.session_id] = session
            self.events.setdefault(session.session_id, [])
            self.interventions.setdefault(session.session_id, [])
        self.append_event(
            session.session_id,
            "session.created",
            "system",
            {
                "session_id": session.session_id,
                "tool_name": tool_name,
                "model": model,
                "status": "queued",
                "observer_url": self.observer_public_url(session.session_id),
                "observer_path": f"/sessions/{session.session_id}",
            },
            parent_session_id=parent_session_id,
            tool_name=tool_name,
        )
        return session

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
            self._write_event(event)
            public_event = self._public_event(event, raw=False)
            self.bus.broadcast(public_event)
            self._cv.notify_all()
            return event

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
            if status == "valid":
                session.valid = True
            elif status in {"invalid", "error"}:
                session.valid = False
            if payload and "message" in payload:
                session.error = str(payload["message"])
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
        with self._lock:
            sessions = [session.to_dict() | {"observer_url": self.observer_url(session.session_id)} for session in self.sessions.values()]
        return sorted(sessions, key=lambda item: item["started_at"], reverse=True)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self.sessions[session_id].to_dict() | {"observer_url": self.observer_url(session_id)}

    def get_events(self, session_id: str, *, raw: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self.events.get(session_id, []))
        return [self._public_event(event, raw=raw) for event in events]

    def export_transcript(self, session_id: str, *, raw: bool = False) -> dict[str, Any]:
        return {
            "session": self.get_session(session_id),
            "events": self.get_events(session_id, raw=raw),
            "raw": raw,
        }

    def build_follow_up_prompt(self, parent_session_id: str, instruction: str) -> str:
        transcript = self.export_transcript(parent_session_id, raw=False)
        session = transcript.get("session", {})
        events = transcript.get("events", [])
        previous_prompt = _last_event_text(events, {"prompt.sent", "prompt.rendered"}, "prompt")
        previous_response = _last_gemini_response(events)
        final_result = _last_final_result(events)
        return (
            "Continue from this previous Gemness observer session. Use only this summarized "
            "observer transcript as context for the user's follow-up; do not claim access to Codex "
            "hidden reasoning.\n\n"
            "Previous session summary:\n"
            f"- Tool: {session.get('tool_name', 'unknown')}\n"
            f"- Model: {session.get('model', 'unknown')}\n"
            f"- Status: {session.get('status', 'unknown')}\n"
            f"- Previous prompt: {_clip(previous_prompt)}\n"
            f"- Gemini response: {_clip(previous_response)}\n"
            f"- Final result: {_clip(final_result)}\n\n"
            f"User follow-up:\n{instruction}"
        )

    def validate_token(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.token)

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
                    self.events.setdefault(event.session_id, []).append(event)
                    self._rebuild_session_from_event(event)
            except (OSError, KeyError, json.JSONDecodeError):
                continue

    def _rebuild_session_from_event(self, event: ObserverEvent) -> None:
        if event.type == "session.created":
            payload = event.payload
            session_id = event.session_id
            self.sessions.setdefault(
                session_id,
                SessionRecord(
                    session_id=session_id,
                    tool_name=str(payload.get("tool_name") or event.tool_name or "unknown"),
                    model=str(payload.get("model") or ""),
                    status=payload.get("status", "queued"),  # type: ignore[arg-type]
                    started_at=event.ts,
                    parent_session_id=event.parent_session_id,
                ),
            )
        session = self.sessions.get(event.session_id)
        if session is None:
            return
        status = event.payload.get("status")
        if isinstance(status, str) and status in SESSION_STATUSES:
            session.status = status  # type: ignore[assignment]
            session.updated_at = event.ts
            if status in FINAL_STATUSES:
                session.completed_at = event.ts
                session.duration_ms = _duration_ms(session.started_at, event.ts)


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
