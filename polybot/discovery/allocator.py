from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.holdings import _atomic_json_write

from .config import AllocatorConfig


@dataclass(frozen=True)
class AllocationRequest:
    market_id: str
    event_slug: str
    correlation_group: str
    deadline_iso: str
    usd: float


class PortfolioAllocator:
    """Portfolio-level exposure control across every discovered market.

    Discovering more markets must not silently multiply correlated risk: every
    prospective order is checked against per-order, per-market, per-event,
    per-correlation-group, per-deadline-week, daily, and total caps plus a
    simultaneous-position limit. State is persisted atomically so the caps
    survive restarts and are shared by every strategy the pipeline arms.
    """

    def __init__(self, state_path: Path, config: AllocatorConfig):
        self.state_path = state_path
        self.config = config

    # -- queries --

    def preview(self, request: AllocationRequest) -> tuple[float, list[str]]:
        """Maximum USD grantable for this request and the blockers that bound
        it to zero. Pure read: nothing is reserved."""
        state = self._load()
        blockers: list[str] = []
        remaining = [self.config.per_order_usd]

        def bound(name: str, cap: float, spent: float) -> None:
            left = cap - spent
            if left <= 0:
                blockers.append(name)
            remaining.append(max(0.0, left))

        bound("per_market_limit", self.config.per_market_usd, _get(state, "per_market", request.market_id))
        bound("per_event_limit", self.config.per_event_usd, _get(state, "per_event", request.event_slug))
        bound("correlation_group_limit", self.config.per_group_usd, _get(state, "per_group", request.correlation_group))
        bound("daily_limit", self.config.daily_usd, _get(state, "per_day", _today()))
        bound("total_limit", self.config.total_usd, float(state.get("total", 0.0)))
        bound("deadline_week_limit", self.config.max_per_deadline_week_usd, _get(state, "per_deadline_week", _week(request.deadline_iso)))

        open_positions = state.get("open_positions", [])
        if request.market_id not in open_positions and len(open_positions) >= self.config.max_open_positions:
            blockers.append("max_open_positions")

        granted = 0.0 if blockers else round(min(min(remaining), request.usd), 2)
        if granted <= 0 and not blockers:
            blockers.append("no_remaining_allowance")
            granted = 0.0
        return granted, blockers

    # -- mutations --

    def commit(self, request: AllocationRequest) -> dict[str, Any]:
        """Reserve exposure for an order that is about to be placed. Fails
        closed if the preview no longer grants the requested amount."""
        granted, blockers = self.preview(request)
        if blockers or granted + 1e-9 < request.usd:
            raise ValueError(f"allocation rejected for {request.market_id}: granted={granted} blockers={blockers}")
        state = self._load()
        _add(state, "per_market", request.market_id, request.usd)
        _add(state, "per_event", request.event_slug, request.usd)
        _add(state, "per_group", request.correlation_group, request.usd)
        _add(state, "per_day", _today(), request.usd)
        _add(state, "per_deadline_week", _week(request.deadline_iso), request.usd)
        state["total"] = round(float(state.get("total", 0.0)) + request.usd, 2)
        open_positions = set(state.get("open_positions", []))
        open_positions.add(request.market_id)
        state["open_positions"] = sorted(open_positions)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save(state)
        return state

    def release_position(self, market_id: str) -> dict[str, Any]:
        """Mark a position closed (spent exposure history is retained; only
        the simultaneous-position slot is freed)."""
        state = self._load()
        state["open_positions"] = sorted(set(state.get("open_positions", [])) - {market_id})
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save(state)
        return state

    def snapshot(self) -> dict[str, Any]:
        return self._load()

    # -- persistence --

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ValueError(f"corrupt allocator state {self.state_path}; refusing to guess remaining budgets")
        return raw if isinstance(raw, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        _atomic_json_write(self.state_path, state)


def _get(state: dict[str, Any], bucket: str, key: str) -> float:
    values = state.get(bucket)
    if not isinstance(values, dict):
        return 0.0
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _add(state: dict[str, Any], bucket: str, key: str, usd: float) -> None:
    values = state.setdefault(bucket, {})
    values[key] = round(_get(state, bucket, key) + usd, 2)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _week(deadline_iso: str) -> str:
    text = (deadline_iso or "").strip().replace("Z", "+00:00")
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    iso = deadline.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"
