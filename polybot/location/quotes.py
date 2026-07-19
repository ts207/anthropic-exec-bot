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

    # A 404 means the book is gone (delisted/closed outcome); it does not
    # come back, so retry rarely. Other failures (timeouts, connection
    # errors) may be transient: back off for minutes, not hours. Without
    # this, a universe sweep re-polled ~2,000 dead books every cycle and
    # the connection-error timeouts alone added ~30 minutes per cycle.
    DEAD_BOOK_BACKOFF_SECONDS = 6 * 3600.0
    TRANSIENT_FAILURE_BACKOFF_SECONDS = 15 * 60.0

    def __init__(self, token_ids: list[str], *, refresh_seconds: float = 2.0):
        self.book = BookCache(token_ids)
        self.refresh_seconds = max(0.0, refresh_seconds)
        self._refreshed_at: dict[str, float] = {}
        self._backoff_until: dict[str, float] = {}

    def quote_snapshot(self, yes_token_id: str) -> dict[str, Any]:
        now = time.monotonic()
        last = self._refreshed_at.get(yes_token_id)
        backoff = self._backoff_until.get(yes_token_id)
        due = last is None or now - last >= self.refresh_seconds
        if due and (backoff is None or now >= backoff):
            try:
                self.book.rest_snapshot(yes_token_id)
                self._backoff_until.pop(yes_token_id, None)
            except Exception as exc:
                # One dead token must not abort the whole discovery/valuation
                # cycle. Serve the possibly-empty cached state; the scanner
                # treats a missing quote as that market's blocker.
                is_gone = "404" in str(exc)
                self._backoff_until[yes_token_id] = now + (
                    self.DEAD_BOOK_BACKOFF_SECONDS if is_gone else self.TRANSIENT_FAILURE_BACKOFF_SECONDS
                )
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
