from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.holdings import _atomic_json_write

from .types import MarketContext, SourcePlan


def _safe_name(market_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", market_id)[:180]


class DiscoveryStore:
    """Durable, atomic JSON records for the market-first pipeline.

    Layout under data_dir:
      contexts/<market_id>.json      -- MarketContext (incl. analysis, state, scores)
      source_plans/<market_id>.json  -- SourcePlan
      allocations.json               -- PortfolioAllocator ledger (owned by allocator)
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.contexts_dir = data_dir / "contexts"
        self.plans_dir = data_dir / "source_plans"

    # -- contexts --

    def save_context(self, context: MarketContext) -> MarketContext:
        stamped = MarketContext.from_dict({**context.as_dict(), "updated_at": _now()})
        _atomic_json_write(self.contexts_dir / f"{_safe_name(context.market_id)}.json", stamped.as_dict())
        return stamped

    def load_context(self, market_id: str) -> MarketContext | None:
        path = self.contexts_dir / f"{_safe_name(market_id)}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MarketContext.from_dict(raw) if isinstance(raw, dict) else None

    def all_contexts(self) -> list[MarketContext]:
        if not self.contexts_dir.exists():
            return []
        contexts: list[MarketContext] = []
        for path in sorted(self.contexts_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                contexts.append(MarketContext.from_dict(raw))
        return contexts

    # -- source plans --

    def save_source_plan(self, plan: SourcePlan) -> SourcePlan:
        stamped = SourcePlan.from_dict({**plan.as_dict(), "created_at": plan.created_at or _now()})
        _atomic_json_write(self.plans_dir / f"{_safe_name(plan.market_id)}.json", stamped.as_dict())
        return stamped

    def load_source_plan(self, market_id: str) -> SourcePlan | None:
        path = self.plans_dir / f"{_safe_name(market_id)}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SourcePlan.from_dict(raw) if isinstance(raw, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["DiscoveryStore"]
