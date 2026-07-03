from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from .config import SETTINGS

_LOCK = Lock()
_ET = ZoneInfo("America/New_York")


def _default_log_path() -> Path:
    SETTINGS.logs_dir.mkdir(parents=True, exist_ok=True)
    return SETTINGS.logs_dir / "polybot.jsonl"


def log_event(event: str, **fields: Any) -> None:
    path = Path(fields.pop("_log_path", _default_log_path()))
    now = datetime.now(timezone.utc)
    record = {
        "ts_utc": now.isoformat(),
        "ts_et": now.astimezone(_ET).isoformat(),
        "ts_mono": time.monotonic(),
        "event": event,
        **fields,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
