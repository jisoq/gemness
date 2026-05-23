from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import GemnessConfig
from .models import utc_now
from .observer import FINAL_STATUSES, ObserverHub, OPEN_SESSION_STATUSES


RunCallable = Callable[
    [threading.Event, Callable[[Any], None], Callable[[dict[str, Any]], None]],
    dict[str, Any],
]


@dataclass(slots=True)
class ManagedRun:
    run_id: str
    idempotency_key: str | None
    idempotency_context: dict[str, Any] | None
    idempotency_lookup_key: str | None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)
    process: Any = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None


class RunManager:
    def __init__(self, config: GemnessConfig, hub: ObserverHub) -> None:
        self.config = config
        self.hub = hub
        self._runs: dict[str, ManagedRun] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()
        self._concurrency_limit = max(1, int(config.agy_concurrency_limit or 1))
        self._queue_limit = max(1, int(config.agy_queue_limit or self._concurrency_limit))
        self._work_queue: queue.Queue[tuple[ManagedRun, RunCallable]] = queue.Queue(maxsize=self._queue_limit)
        self._shutdown = threading.Event()
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"gemness-run-worker-{index + 1}", daemon=True)
            for index in range(self._concurrency_limit)
        ]
        for worker in self._workers:
            worker.start()

    def find_by_idempotency_key(self, idempotency_key: str | None, *, idempotency_context: dict[str, Any] | None = None) -> str | None:
        key = _clean_key(idempotency_key)
        if key is None:
            return None
        lookup_key = _lookup_key(key, idempotency_context)
        with self._lock:
            run_id = self._idempotency.get(lookup_key)
        if run_id is not None:
            return run_id
        if idempotency_context and idempotency_context.get("workspace_fingerprint_degraded") is True:
            return None
        self.hub.refresh_from_disk()
        for session in self.hub.list_sessions():
            session_id = session.get("session_id")
            if not isinstance(session_id, str):
                continue
            for event in self.hub.get_events(session_id, raw=True):
                if event.get("type") != "run.accepted":
                    continue
                payload = event.get("payload", {})
                if isinstance(payload, dict) and _payload_matches_idempotency(payload, key, lookup_key, idempotency_context):
                    with self._lock:
                        self._idempotency[lookup_key] = session_id
                    return session_id
        return None

    def start(
        self,
        run_id: str,
        run_callable: RunCallable,
        *,
        idempotency_key: str | None = None,
        idempotency_context: dict[str, Any] | None = None,
    ) -> ManagedRun:
        clean_key = _clean_key(idempotency_key)
        clean_context = _clean_context(idempotency_context)
        lookup_key = _lookup_key(clean_key, clean_context) if clean_key is not None else None
        managed = ManagedRun(
            run_id=run_id,
            idempotency_key=clean_key,
            idempotency_context=clean_context,
            idempotency_lookup_key=lookup_key,
        )
        with self._lock:
            self._runs[run_id] = managed
            if managed.idempotency_lookup_key:
                self._idempotency.setdefault(managed.idempotency_lookup_key, run_id)
        try:
            self._work_queue.put_nowait((managed, run_callable))
        except queue.Full:
            self._reject_queue_full(managed)
            return managed
        self.hub.append_event(
            run_id,
            "run.accepted",
            "system",
            {
                "run_id": run_id,
                "idempotency_key": managed.idempotency_key,
                "idempotency_context": managed.idempotency_context,
                "idempotency_scope": managed.idempotency_lookup_key,
                "concurrency_limit": self._concurrency_limit,
                "queue_limit": self._queue_limit,
            },
        )
        return managed

    def get(self, run_id: str) -> ManagedRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def is_managed(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def cancel(self, run_id: str) -> dict[str, Any]:
        managed = self.get(run_id)
        if managed is None:
            return self._cancel_unmanaged(run_id)
        if managed.result is not None:
            return {
                "status": str(managed.result.get("status") or "completed"),
                "run_id": run_id,
                "message": "Run is already terminal.",
            }
        managed.cancel_event.set()
        self.hub.append_event(run_id, "run.cancel_requested", "system", {"run_id": run_id})
        process = managed.process
        if process is not None:
            try:
                process.terminate()
            except Exception as exc:  # noqa: BLE001 - best-effort cancellation surface.
                self.hub.append_event(run_id, "run.cancel_failed", "system", {"run_id": run_id, "message": str(exc)})
        with managed.condition:
            if managed.result is None:
                self._cancel_if_still_queued(run_id)
        return {"status": "cancelling", "run_id": run_id}

    def await_run(self, run_id: str, timeout_sec: float) -> ManagedRun | None:
        managed = self.get(run_id)
        if managed is None:
            return None
        timeout_sec = max(0.0, min(float(timeout_sec), 30.0))
        deadline = time.monotonic() + timeout_sec
        with managed.condition:
            while managed.result is None and not managed.done.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                managed.condition.wait(timeout=remaining)
        return managed

    def register_process(self, run_id: str, process: Any) -> None:
        managed = self.get(run_id)
        if managed is None:
            return
        with managed.condition:
            managed.process = process
            managed.condition.notify_all()

    def heartbeat(self, run_id: str, payload: dict[str, Any]) -> None:
        managed = self.get(run_id)
        payload = {"run_id": run_id, **payload}
        if managed is not None and managed.cancel_event.is_set():
            payload["cancel_requested"] = True
        self.hub.append_event(run_id, "antigravity.heartbeat", "gemness", payload)

    def shutdown(self) -> None:
        self._shutdown.set()
        for worker in self._workers:
            worker.join(timeout=1)

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                managed, run_callable = self._work_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._run_managed(managed, run_callable)
            finally:
                self._work_queue.task_done()

    def _run_managed(self, managed: ManagedRun, run_callable: RunCallable) -> None:
        try:
            if managed.cancel_event.is_set():
                managed.result = self._mark_cancelled(managed.run_id, "cancelled_before_start")
                return
            result = run_callable(
                managed.cancel_event,
                lambda process: self.register_process(managed.run_id, process),
                lambda payload: self.heartbeat(managed.run_id, payload),
            )
            managed.result = result
        except Exception as exc:  # noqa: BLE001 - detached worker must settle its public run.
            managed.error = str(exc)
            managed.result = {
                "status": "error",
                "run_id": managed.run_id,
                "session_id": managed.run_id,
                "message": str(exc),
                "observer_url": self.hub.observer_url(managed.run_id),
            }
            try:
                self.hub.set_status(managed.run_id, "error", "session.error", managed.result)
            except Exception:
                pass
        finally:
            self._settle_managed(managed)

    def _reject_queue_full(self, managed: ManagedRun) -> None:
        payload = {
            "status": "error",
            "run_id": managed.run_id,
            "session_id": managed.run_id,
            "observer_url": self.hub.observer_url(managed.run_id),
            "message": f"Antigravity run queue is full; queue_limit={self._queue_limit}.",
            "reason": "run_queue_full",
            "queue_limit": self._queue_limit,
        }
        managed.result = payload
        self.hub.append_event(
            managed.run_id,
            "run.rejected",
            "system",
            {"run_id": managed.run_id, "idempotency_key": managed.idempotency_key, "reason": "run_queue_full", "queue_limit": self._queue_limit},
        )
        self.hub.set_status(managed.run_id, "error", "session.error", payload)
        self._settle_managed(managed)

    def _settle_managed(self, managed: ManagedRun) -> None:
        managed.completed_at = utc_now()
        managed.done.set()
        with managed.condition:
            managed.condition.notify_all()
        self._evict(managed)

    def _evict(self, managed: ManagedRun) -> None:
        with self._lock:
            self._runs.pop(managed.run_id, None)
            if managed.idempotency_lookup_key and self._idempotency.get(managed.idempotency_lookup_key) == managed.run_id:
                self._idempotency.pop(managed.idempotency_lookup_key, None)

    def _cancel_unmanaged(self, run_id: str) -> dict[str, Any]:
        try:
            session = self.hub.get_session(run_id, raw=True)
        except KeyError:
            return {"status": "error", "run_id": run_id, "message": f"Unknown run_id: {run_id}"}
        if session.get("status") in FINAL_STATUSES:
            return {"status": session["status"], "run_id": run_id, "message": "Run is already terminal."}
        payload = {
            "status": "cancelled",
            "run_id": run_id,
            "session_id": run_id,
            "observer_url": self.hub.observer_url(run_id),
            "reason": "cancel_requested_after_manager_restart",
        }
        self.hub.append_event(run_id, "run.cancel_requested", "system", {"run_id": run_id, "managed": False})
        self.hub.set_status(run_id, "cancelled", "session.cancelled", payload)
        return payload

    def _cancel_if_still_queued(self, run_id: str) -> None:
        try:
            session = self.hub.get_session(run_id, raw=True)
        except KeyError:
            return
        if session.get("status") not in OPEN_SESSION_STATUSES:
            return
        if session.get("status") != "queued":
            return
        self._mark_cancelled(run_id, "cancelled_while_queued")

    def _mark_cancelled(self, run_id: str, reason: str) -> dict[str, Any]:
        payload = {
            "status": "cancelled",
            "run_id": run_id,
            "session_id": run_id,
            "observer_url": self.hub.observer_url(run_id),
            "reason": reason,
        }
        self.hub.set_status(run_id, "cancelled", "session.cancelled", payload)
        return payload


def _clean_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    key = " ".join(str(idempotency_key).split())
    return key or None


def _clean_context(idempotency_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(idempotency_context, dict):
        return None
    clean = {str(key): value for key, value in idempotency_context.items() if value is not None}
    return clean or None


def _lookup_key(idempotency_key: str | None, idempotency_context: dict[str, Any] | None) -> str | None:
    if idempotency_key is None:
        return None
    if not idempotency_context:
        return idempotency_key
    payload = {"idempotency_key": idempotency_key, "context": idempotency_context}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"scoped:{hashlib.sha256(serialized.encode('utf-8', errors='replace')).hexdigest()}"


def _payload_matches_idempotency(
    payload: dict[str, Any],
    idempotency_key: str,
    lookup_key: str | None,
    idempotency_context: dict[str, Any] | None,
) -> bool:
    if payload.get("idempotency_key") != idempotency_key:
        return False
    if not idempotency_context:
        return payload.get("idempotency_scope") in {None, lookup_key}
    return payload.get("idempotency_scope") == lookup_key
