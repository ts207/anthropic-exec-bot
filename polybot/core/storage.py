from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"FLIPPED", "NO_SOLD_YES_SKIPPED", "YES_SOLD_NO_SKIPPED", "EXITED", "FLIP_INCOMPLETE", "STOPPED"}

# Non-terminal states that must survive being overwritten in state.json, because
# later decisions consult them: TRIMMED gates duplicate trims, and the scheduled
# hold signal suspends time-decay selling for a window.
MARKER_STATES = TERMINAL_STATES | {"TRIMMED", "YES_SCHEDULED_HOLD_SIGNAL"}


@dataclass(frozen=True)
class StateRecord:
    state: str
    updated_at: str
    payload: dict[str, Any]


class StateStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_path = data_dir / "state.json"

    def current(self) -> StateRecord | None:
        if not self.state_path.exists():
            return None
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return StateRecord(
            state=str(raw.get("state") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
        )

    def terminal_state(self) -> StateRecord | None:
        record = self.current()
        if record and record.state in TERMINAL_STATES:
            return record
        return None

    def marker(self, state: str) -> StateRecord | None:
        marker_path = self.data_dir / f"{state}.json"
        if not marker_path.exists():
            return None
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return StateRecord(
            state=str(raw.get("state") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
        )

    def write(self, state: str, **payload: Any) -> StateRecord:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        record = StateRecord(
            state=state,
            updated_at=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        encoded = json.dumps(asdict(record), indent=2, sort_keys=True, default=str)
        self.state_path.write_text(encoded + "\n", encoding="utf-8")
        if state in MARKER_STATES:
            (self.data_dir / f"{state}.json").write_text(encoded + "\n", encoding="utf-8")
        if state == "NO_SOLD_YES_SKIPPED":
            (self.data_dir / "NO_SOLD.json").write_text(encoded + "\n", encoding="utf-8")
        return record


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")

__all__ = ["MARKER_STATES", "TERMINAL_STATES", "StateRecord", "StateStore", "append_jsonl"]
