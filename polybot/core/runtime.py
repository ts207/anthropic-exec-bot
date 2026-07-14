from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .holdings import _atomic_json_write


class ReconciliationError(RuntimeError):
    pass


class ProcessLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class JournalRecord:
    execution_id: str
    action: str
    phase: str
    created_at: str
    updated_at: str
    payload: dict[str, Any]


class ExecutionJournal:
    """Append-by-replacement execution journal for restart diagnosis.

    The wallet is still authoritative.  The journal establishes which mutation
    may have been in flight when a process died so startup reconciliation can
    report and repair the local holding rather than guessing.
    """

    def __init__(self, data_dir: Path):
        self.root = data_dir / "execution_journal"

    def start(self, action: str, **payload: Any) -> JournalRecord:
        now = datetime.now(timezone.utc).isoformat()
        record = JournalRecord(uuid.uuid4().hex, action, "decision_created", now, now, payload)
        self._write(record)
        return record

    def update(self, record: JournalRecord, phase: str, **payload: Any) -> JournalRecord:
        updated = JournalRecord(
            record.execution_id,
            record.action,
            phase,
            record.created_at,
            datetime.now(timezone.utc).isoformat(),
            {**record.payload, **payload},
        )
        self._write(updated)
        return updated

    def incomplete(self) -> list[JournalRecord]:
        if not self.root.exists():
            return []
        records: list[JournalRecord] = []
        for path in sorted(self.root.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = JournalRecord(
                execution_id=str(raw["execution_id"]),
                action=str(raw["action"]),
                phase=str(raw["phase"]),
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
            )
            if record.phase not in {"completed", "unfilled", "blocked", "failed"}:
                records.append(record)
        return records

    def _write(self, record: JournalRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(self.root / f"{record.execution_id}.json", asdict(record))


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._owned = False

    def acquire(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}) + "\n"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pid = self._existing_pid()
            if pid is not None and _pid_alive(pid):
                raise ProcessLockError(f"location bot already running with pid {pid}")
            self.path.unlink(missing_ok=True)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise ProcessLockError("location bot process lock was claimed concurrently") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self._owned = True
        return self

    def release(self) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *_args: Any) -> None:
        self.release()

    def _existing_pid(self) -> int | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return int(raw.get("pid")) if isinstance(raw, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


__all__ = [
    "ExecutionJournal",
    "JournalRecord",
    "ProcessLock",
    "ProcessLockError",
    "ReconciliationError",
]
