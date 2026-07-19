from __future__ import annotations

import time
from typing import Any, Protocol

from polybot.book import BookCache
from polybot.log import log_event


class QuoteAdapter(Protocol):
    """Read-only surface used by the paper forecaster."""

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        ...

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        ...


class PublicClobQuoteAdapter:
    """Public CLOB books with no credential or order-submission surface.

    Keeping paper quotes in a type that has no mutation methods makes the
    anticipatory engine technically incapable of submitting an order even
    when the confirmation strategy is running in dry-run mode.
    """

    def __init__(self, token_ids: list[str], *, refresh_seconds: float = 2.0):
        self.book = BookCache(token_ids)
        self.refresh_seconds = max(0.0, refresh_seconds)
        self._refreshed_at: dict[str, float] = {}

    def quote_snapshot(self, yes_token_id: str) -> dict[str, Any]:
        now = time.monotonic()
        last = self._refreshed_at.get(yes_token_id)
        if last is None or now - last >= self.refresh_seconds:
            try:
                self.book.rest_snapshot(yes_token_id)
            except Exception as exc:
                # A delisted/closed market's book 404s forever; one dead
                # token must not abort the whole discovery/valuation cycle
                # (observed: every hourly cycle died on the same 404 for
                # 12h straight). Serve the possibly-empty cached state; the
                # scanner treats a missing quote as that market's blocker.
                log_event("quote_snapshot_failed", token_id=str(yes_token_id), error=str(exc))
            self._refreshed_at[yes_token_id] = time.monotonic()
        snapshot = self.book.snapshot_state(yes_token_id)
        snapshot["source"] = "public_clob_book"
        return snapshot

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        value = self.quote_snapshot(yes_token_id).get("best_ask")
        return float(value) if value is not None else None

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        value = self.quote_snapshot(yes_token_id).get("best_bid")
        return float(value) if value is not None else None


class QuoteOnlyFacade:
    """Expose only quote reads from an execution-capable adapter."""

    def __init__(self, adapter: QuoteAdapter):
        self._adapter = adapter

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._adapter.yes_best_ask(yes_token_id)

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._adapter.yes_best_bid(yes_token_id)


__all__ = ["PublicClobQuoteAdapter", "QuoteAdapter", "QuoteOnlyFacade"]
