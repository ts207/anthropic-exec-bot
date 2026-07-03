from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SourceReading:
    value: float
    unit: str
    is_locked: bool
    confidence: float
    raw_url: str
    fetched_at: datetime
    raw_value: float | None = None


class SourceAdapter(Protocol):
    def poll(self) -> SourceReading | None:
        ...
