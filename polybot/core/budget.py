from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ClassifierConfig


class ClassifierBudgetStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "classifier_budget.json"

    def block_reason(self, config: ClassifierConfig) -> str | None:
        now = datetime.now(timezone.utc)
        raw = self._read()
        hour_key = self._hour_key(now)
        day_key = self._day_key(now)
        if config.max_classifier_errors_per_hour > 0 and raw.get("errors_by_hour", {}).get(hour_key, 0) >= config.max_classifier_errors_per_hour:
            return "classifier_error_cap_exceeded"
        if config.max_escalations_per_hour > 0 and raw.get("attempts_by_hour", {}).get(hour_key, 0) >= config.max_escalations_per_hour:
            return "classifier_budget_exhausted_hourly"
        if config.max_escalations_per_day > 0 and raw.get("attempts_by_day", {}).get(day_key, 0) >= config.max_escalations_per_day:
            return "classifier_budget_exhausted_daily"
        return None

    def record_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        self._increment("attempts_by_hour", self._hour_key(now))
        self._increment("attempts_by_day", self._day_key(now))

    def record_error(self) -> None:
        now = datetime.now(timezone.utc)
        self._increment("errors_by_hour", self._hour_key(now))

    def mark_notified_once(self, reason: str, window: str) -> bool:
        now = datetime.now(timezone.utc)
        key = self._hour_key(now) if window == "hour" else self._day_key(now)
        raw = self._read()
        notified = raw.setdefault("notified", {})
        reason_notified = notified.setdefault(reason, [])
        if key in reason_notified:
            return False
        reason_notified.append(key)
        self._write(raw)
        return True

    def status(self, config: ClassifierConfig) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        raw = self._read()
        hour_key = self._hour_key(now)
        day_key = self._day_key(now)
        return {
            "hour_key": hour_key,
            "day_key": day_key,
            "attempts_this_hour": raw.get("attempts_by_hour", {}).get(hour_key, 0),
            "attempts_today": raw.get("attempts_by_day", {}).get(day_key, 0),
            "errors_this_hour": raw.get("errors_by_hour", {}).get(hour_key, 0),
            "max_escalations_per_hour": config.max_escalations_per_hour,
            "max_escalations_per_day": config.max_escalations_per_day,
            "max_classifier_errors_per_hour": config.max_classifier_errors_per_hour,
            "block_reason": self.block_reason(config),
        }

    def _increment(self, bucket: str, key: str) -> None:
        raw = self._read()
        values = raw.setdefault(bucket, {})
        values[key] = int(values.get(key, 0)) + 1
        self._write(raw)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def _write(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @staticmethod
    def _hour_key(now: datetime) -> str:
        return now.strftime("%Y%m%d%H")

    @staticmethod
    def _day_key(now: datetime) -> str:
        return now.strftime("%Y%m%d")

