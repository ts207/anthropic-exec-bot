from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .holdings import _atomic_json_write


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime | None:
    text = (stamp or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


class SecondSourceGate:
    """Pre-entry gate for large orders: an entry whose notional exceeds the
    configured threshold only executes once a SECOND independent domain has
    produced a qualifying trigger for the same target inside the freshness
    window. The first trigger is recorded and the entry deferred -- a single
    fabricated or wrong wire story then costs an alert, not the full stake."""

    def __init__(self, data_dir: Path, window_minutes: float):
        self.path = Path(data_dir) / "entry_confirmations.json"
        self.window_minutes = window_minutes

    def confirm(self, key: str, domain: str) -> bool:
        """True when an independent fresh confirmation already exists for this
        entry key (the entry may proceed). Otherwise records this trigger as
        the pending first confirmation and returns False."""
        records = _load_json(self.path)
        existing = records.get(key)
        now = _now()
        if isinstance(existing, dict):
            recorded_domain = str(existing.get("domain") or "")
            recorded_at = _parse(str(existing.get("at") or ""))
            fresh = recorded_at is not None and (now - recorded_at) <= timedelta(minutes=self.window_minutes)
            if fresh and recorded_domain and recorded_domain != domain:
                return True
            if fresh and recorded_domain == domain:
                # Same outlet repeating itself is not independent confirmation;
                # keep the earlier timestamp so the window does not roll.
                return False
        records[key] = {"domain": domain, "at": now.isoformat()}
        _atomic_json_write(self.path, records)
        return False


class CorroborationTracker:
    """Post-entry corroboration: after an autonomous entry, a second
    independent domain must reinforce the thesis within the deadline;
    otherwise the operator is alerted (or the position trimmed, per config).
    A position standing on one uncorroborated story is the single largest
    tail risk of confirmed-entry trading."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "corroboration.json"

    def start(self, *, entry_domain: str, minutes: float, action: str) -> None:
        deadline = _now() + timedelta(minutes=minutes)
        _atomic_json_write(
            self.path,
            {
                "entry_domain": entry_domain,
                "deadline": deadline.isoformat(),
                "action": action,
                "satisfied": False,
                "escalated": False,
                "started_at": _now().isoformat(),
            },
        )

    def pending(self) -> dict[str, Any] | None:
        record = _load_json(self.path)
        if not record or record.get("satisfied") or record.get("escalated"):
            return None
        return record

    def satisfy(self, domain: str) -> bool:
        """Mark corroborated when a DIFFERENT domain reinforces the thesis."""
        record = self.pending()
        if record is None:
            return False
        if not domain or domain == str(record.get("entry_domain") or ""):
            return False
        record["satisfied"] = True
        record["satisfied_by"] = domain
        record["satisfied_at"] = _now().isoformat()
        _atomic_json_write(self.path, record)
        return True

    def overdue(self) -> dict[str, Any] | None:
        record = self.pending()
        if record is None:
            return None
        deadline = _parse(str(record.get("deadline") or ""))
        if deadline is None or _now() >= deadline:
            return record
        return None

    def mark_escalated(self) -> None:
        record = _load_json(self.path)
        if not record:
            return
        record["escalated"] = True
        record["escalated_at"] = _now().isoformat()
        _atomic_json_write(self.path, record)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


__all__ = ["SecondSourceGate", "CorroborationTracker"]
