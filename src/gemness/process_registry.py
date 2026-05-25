from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import GemnessConfig
from .mcp_metadata import SERVER_VERSION


REGISTRY_STALE_SEC = 30.0


@dataclass(slots=True)
class ProcessRecord:
    pid: int
    parent_pid: int | None
    started_at: float
    last_seen_at: float
    argv: list[str]
    cwd: str
    workspace_id: str
    transcript_dir: str
    observer_host: str
    observer_port: int
    observer_mode: str
    owns_observer: bool
    version: str
    observer_error: str | None = None
    attached_to: str | None = None
    management_token: str | None = None
    registry_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessRecord":
        return cls(
            pid=int(data["pid"]),
            parent_pid=int(data["parent_pid"]) if data.get("parent_pid") is not None else None,
            started_at=float(data.get("started_at") or time.time()),
            last_seen_at=float(data.get("last_seen_at") or data.get("started_at") or time.time()),
            argv=[str(item) for item in data.get("argv", []) if item is not None],
            cwd=str(data.get("cwd") or ""),
            workspace_id=str(data.get("workspace_id") or workspace_id_for_path(Path(data.get("cwd") or "."))),
            transcript_dir=str(data.get("transcript_dir") or ""),
            observer_host=str(data.get("observer_host") or "127.0.0.1"),
            observer_port=int(data.get("observer_port") or 0),
            observer_mode=str(data.get("observer_mode") or "unknown"),
            owns_observer=bool(data.get("owns_observer", False)),
            version=str(data.get("version") or "unknown"),
            observer_error=data.get("observer_error") if isinstance(data.get("observer_error"), str) else None,
            attached_to=data.get("attached_to") if isinstance(data.get("attached_to"), str) else None,
            management_token=data.get("management_token") if isinstance(data.get("management_token"), str) else None,
            registry_id=str(data.get("registry_id") or ""),
        )

    def to_dict(self, *, include_token: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_token:
            data.pop("management_token", None)
            data["has_management_token"] = bool(self.management_token)
        return {key: value for key, value in data.items() if value is not None}


class ProcessRegistry:
    def __init__(self, config: GemnessConfig) -> None:
        self.config = config
        self.registry_dir = Path(config.process_registry_dir).expanduser()
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.pid = os.getpid()
        self.registry_id = uuid.uuid4().hex
        self.path = self.registry_dir / f"{self.pid}-{self.registry_id}.json"
        self._started_at = time.time()

    def write_current(
        self,
        *,
        observer_mode: str,
        owns_observer: bool,
        observer_error: str | None = None,
        attached_to: str | None = None,
        management_token: str | None = None,
    ) -> ProcessRecord:
        existing = self.read_current()
        record = ProcessRecord(
            pid=self.pid,
            parent_pid=os.getppid(),
            started_at=existing.started_at if existing is not None else self._started_at,
            last_seen_at=time.time(),
            argv=[str(item) for item in sys.argv],
            cwd=str(Path.cwd()),
            workspace_id=workspace_id_for_path(self.config.workspace_root or Path.cwd()),
            transcript_dir=str(Path(self.config.transcript_dir).expanduser()),
            observer_host=self.config.observer_host,
            observer_port=int(self.config.observer_port),
            observer_mode=observer_mode,
            owns_observer=owns_observer,
            observer_error=observer_error,
            attached_to=attached_to,
            version=SERVER_VERSION,
            management_token=management_token,
            registry_id=self.registry_id,
        )
        self._write_record(record)
        return record

    def touch_current(self) -> None:
        record = self.read_current()
        if record is None:
            return
        record.last_seen_at = time.time()
        self._write_record(record)

    def remove_current(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def read_pid(self, pid: int) -> ProcessRecord | None:
        candidates = [record for record in self.list_records() if record.pid == pid]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (not item.owns_observer, -item.last_seen_at))[0]

    def read_current(self) -> ProcessRecord | None:
        path = self.path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return ProcessRecord.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def list_records(self) -> list[ProcessRecord]:
        records = [record for _, record in self._read_records_with_paths()]
        return sorted(records, key=lambda item: item.started_at)

    def _read_records_with_paths(self) -> list[tuple[Path, ProcessRecord]]:
        records: list[tuple[Path, ProcessRecord]] = []
        for path in self.registry_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                records.append((path, ProcessRecord.from_dict(data)))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(records, key=lambda item: item[1].started_at)

    def observer_owner_records(self, host: str, port: int) -> list[ProcessRecord]:
        return [
            record
            for record in self.list_records()
            if record.owns_observer and record.observer_host == host and record.observer_port == port
        ]

    def cleanup(self, *, stale: bool = True, terminate_orphans: bool = False) -> dict[str, Any]:
        removed: list[int] = []
        terminated: list[int] = []
        errors: list[dict[str, Any]] = []
        now = time.time()
        for path, record in self._read_records_with_paths():
            should_remove = stale and (not process_is_running(record.pid))
            should_terminate = terminate_orphans and process_is_running(record.pid) and record_is_orphan(record)
            if should_terminate:
                try:
                    terminate_process(record.pid)
                    terminated.append(record.pid)
                    should_remove = True
                except OSError as exc:
                    errors.append({"pid": record.pid, "error": str(exc)})
            if not should_remove and stale and record.last_seen_at < now - REGISTRY_STALE_SEC and not process_is_running(record.pid):
                should_remove = True
            if should_remove:
                try:
                    path.unlink()
                    removed.append(record.pid)
                except FileNotFoundError:
                    removed.append(record.pid)
                except OSError as exc:
                    errors.append({"pid": record.pid, "error": str(exc)})
        return {"removed": removed, "terminated": terminated, "errors": errors}

    def status(self) -> dict[str, Any]:
        now = time.time()
        records = self.list_records()
        return {
            "registry_dir": str(self.registry_dir),
            "records": [
                record.to_dict(include_token=False)
                | {
                    "running": process_is_running(record.pid),
                    "orphan": record_is_orphan(record),
                    "stale": record.last_seen_at < now - REGISTRY_STALE_SEC,
                }
                for record in records
            ],
        }

    def _write_record(self, record: ProcessRecord) -> None:
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp_path.chmod(0o600)
        except OSError:
            pass
        tmp_path.replace(self.path)


def workspace_id_for_path(path: Path | str | None) -> str:
    raw = str(path or "")
    try:
        raw = str(Path(raw or ".").expanduser().resolve())
    except OSError:
        pass
    normalized = raw.lower() if os.name == "nt" else raw
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"ws_{digest}"


def record_is_orphan(record: ProcessRecord) -> bool:
    return bool(record.parent_pid and record.parent_pid > 0 and not process_is_running(record.parent_pid))


def record_is_takeover_eligible(record: ProcessRecord) -> bool:
    if not process_is_running(record.pid):
        return True
    return record_is_orphan(record) or record.last_seen_at < time.time() - REGISTRY_STALE_SEC


def process_is_running(pid: int) -> bool:
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


def terminate_process(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        raise OSError(f"Refusing to terminate pid {pid}")
    os.kill(pid, signal.SIGTERM)


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
