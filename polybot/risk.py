from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .config import SETTINGS, Guardrails
from .log import log_event


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    path: Path = SETTINGS.risk_state_path
    guardrails: Guardrails = SETTINGS.guardrails
    per_market_spent: dict[str, float] = field(default_factory=dict)
    per_day_spent: dict[str, float] = field(default_factory=dict)
    traded_markets: set[str] = field(default_factory=set)
    dry_run_traded_markets: set[str] = field(default_factory=set)
    consecutive_failures: int = 0
    halted: bool = False
    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path = SETTINGS.risk_state_path, guardrails: Guardrails = SETTINGS.guardrails) -> "RiskState":
        if not path.exists():
            return cls(path=path, guardrails=guardrails)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            guardrails=guardrails,
            per_market_spent={str(k): float(v) for k, v in data.get("per_market_spent", {}).items()},
            per_day_spent={str(k): float(v) for k, v in data.get("per_day_spent", {}).items()},
            traded_markets=set(data.get("traded_markets", [])),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            halted=bool(data.get("halted", False)),
        )

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                {
                    "per_market_spent": self.per_market_spent,
                    "per_day_spent": self.per_day_spent,
                    "traded_markets": sorted(self.traded_markets),
                    "consecutive_failures": self.consecutive_failures,
                    "halted": self.halted,
                },
                indent=2,
                sort_keys=True,
            ) + "\n"
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(encoded, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def remaining_for_market(self, market_key: str) -> float:
        with self._lock:
            return max(0.0, self.guardrails.per_market_notional - self.per_market_spent.get(market_key, 0.0))

    def remaining_for_day(self) -> float:
        with self._lock:
            return max(0.0, self.guardrails.per_day_notional - self.per_day_spent.get(_today_key(), 0.0))

    def allowed_notional(self, market_key: str) -> float:
        with self._lock:
            if self.halted:
                return 0.0
            if market_key in self.traded_markets or market_key in self.dry_run_traded_markets:
                return 0.0
            return min(self.guardrails.per_order_notional, self.remaining_for_market(market_key), self.remaining_for_day())

    def record_spend(self, market_key: str, notional: float) -> None:
        with self._lock:
            today = _today_key()
            self.per_market_spent[market_key] = self.per_market_spent.get(market_key, 0.0) + notional
            self.per_day_spent[today] = self.per_day_spent.get(today, 0.0) + notional
            self.traded_markets.add(market_key)
            self.save()
        log_event(
            "risk_state_update",
            mutation="record_spend",
            market_key=market_key,
            notional=notional,
            market_spent=self.per_market_spent[market_key],
            day_spent=self.per_day_spent[today],
        )

    def reserve_order_attempt(self, market_key: str, notional: float) -> None:
        self.record_spend(market_key, notional)
        log_event("risk_state_update", mutation="reserve_order_attempt", market_key=market_key, notional=notional)

    def record_dry_run_attempt(self, market_key: str) -> None:
        with self._lock:
            self.dry_run_traded_markets.add(market_key)
        log_event("risk_state_update", mutation="dry_run_attempt", market_key=market_key)

    def record_settlement_failure(self, market_key: str | None, reason: str) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.guardrails.kill_switch_failures:
                self.halted = True
            self.save()
        log_event(
            "risk_state_update",
            mutation="settlement_failure",
            market_key=market_key,
            reason=reason,
            consecutive_failures=self.consecutive_failures,
            halted=self.halted,
        )

    def record_settlement_success(self, market_key: str | None) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.save()
        log_event(
            "risk_state_update",
            mutation="settlement_success",
            market_key=market_key,
            consecutive_failures=self.consecutive_failures,
            halted=self.halted,
        )
