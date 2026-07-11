from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HoldingRecord:
    # None means flat: the bot holds nothing on this market. For the location
    # bot the value is the held outcome key (e.g. "qatar"); for the binary
    # rule bot it is the held side ("yes"/"no").
    held_location: str | None
    source: str  # "config_default" | "entry" | "rotation" | "flip" | "exit" | "rotation_incomplete" | "flip_incomplete"
    updated_at: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HoldingsStore:
    """Persistent record of WHAT the bot currently holds on its market.

    The config's held value is only the initial/default holding. Once
    automated entry, rotation, or a flip runs, the live holding diverges from
    the config, and every later decision (protection sells, time decay, price
    alerts, the classifier prompt) must follow the live holding, not the
    config. A missing file means "as configured"; an explicit record always
    wins -- including an explicit flat record after an exit, so a config that
    still names the sold holding cannot resurrect it as held.
    """

    def __init__(self, data_dir: Path, default_held: str | None):
        self.data_dir = data_dir
        self.path = data_dir / "holdings.json"
        self.default_held = _normalize(default_held)

    def record(self) -> HoldingRecord:
        if not self.path.exists():
            return HoldingRecord(self.default_held, "config_default", "", {})
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Fail closed rather than silently reverting to the config default:
            # a corrupt holdings file after an entry/rotation could otherwise
            # make the bot defend (or re-enter) the wrong leg.
            raise ValueError(f"corrupt holdings file {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"holdings file {self.path} must contain an object")
        held = raw.get("held_location")
        return HoldingRecord(
            held_location=_normalize(held) if isinstance(held, str) else None,
            source=str(raw.get("source") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
        )

    def held_location(self) -> str | None:
        return self.record().held_location

    def set_held(self, outcome: str, *, source: str, **payload: Any) -> HoldingRecord:
        return self._write(_normalize(outcome), source, payload)

    def clear_held(self, *, source: str, **payload: Any) -> HoldingRecord:
        return self._write(None, source, payload)

    def _write(self, held: str | None, source: str, payload: dict[str, Any]) -> HoldingRecord:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        record = HoldingRecord(
            held_location=held,
            source=source,
            updated_at=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        self.path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return record


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower().replace(" ", "_")
    return cleaned or None


__all__ = ["HoldingRecord", "HoldingsStore"]
