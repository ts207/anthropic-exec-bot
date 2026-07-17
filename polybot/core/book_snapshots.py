from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from polybot.config import SETTINGS
from polybot.log import log_event

from .storage import append_jsonl

# Event-anchored order-book capture. The system already fetches full depth
# and keeps only best bid/ask; this persists the book at exactly the moments
# that are analytically valuable and nowhere else:
#   gate_escalation   an article cleared the keyword gate (the repricing
#                     window the whole strategy lives in starts here)
#   pre_order         a trade decision is about to hit the book
#   post_execution    right after the fill (impact + how fast it repriced)
# Snapshots NEVER raise into the trading path -- a book-logging failure costs
# a data point, not a trade. Enabled per bot via sources.log_book_snapshots
# (off by default; emitted fleet configs turn it on).

# token_id -> raw CLOB /book payload (dict with asks/bids level lists)
BookFetcher = Callable[[str], dict[str, Any]]


def _http_book_fetch(token_id: str) -> dict[str, Any]:
    import requests

    response = requests.get(f"{SETTINGS.clob_host.rstrip('/')}/book", params={"token_id": token_id}, timeout=10)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


class BookSnapshotLogger:
    def __init__(self, data_dir: Path, *, fetcher: BookFetcher | None = None, max_levels: int = 10):
        self.path = Path(data_dir) / "book_snapshots.jsonl"
        self.fetcher = fetcher or _http_book_fetch
        self.max_levels = max_levels

    def snapshot(self, token_ids: list[str], *, moment: str, **context: Any) -> None:
        """One line per token: timestamped top-N depth plus caller context
        (article hash, execution id, action...). Swallows every error."""
        at = datetime.now(timezone.utc).isoformat()
        for token_id in token_ids:
            if not token_id:
                continue
            try:
                record = self._book_record(str(token_id))
            except Exception as exc:
                log_event("book_snapshot_failed", token_id=str(token_id), moment=moment, error=str(exc))
                continue
            try:
                append_jsonl(self.path, {"at": at, "moment": moment, **record, **context})
            except Exception as exc:
                log_event("book_snapshot_write_failed", token_id=str(token_id), moment=moment, error=str(exc))

    def _book_record(self, token_id: str) -> dict[str, Any]:
        from polybot.book import _levels

        payload = self.fetcher(token_id)
        asks = sorted(_levels(payload.get("asks")), key=lambda level: level[0])[: self.max_levels]
        bids = sorted(_levels(payload.get("bids")), key=lambda level: level[0], reverse=True)[: self.max_levels]
        best_ask = asks[0][0] if asks else None
        best_bid = bids[0][0] if bids else None
        return {
            "token_id": token_id,
            "best_ask": best_ask,
            "best_bid": best_bid,
            "spread": round(best_ask - best_bid, 4) if best_ask is not None and best_bid is not None else None,
            "ask_depth_usd": round(sum(price * size for price, size in asks), 2),
            "bid_depth_usd": round(sum(price * size for price, size in bids), 2),
            "asks": asks,
            "bids": bids,
        }


class NullBookSnapshotLogger:
    """Disabled variant: same surface, no fetches, no writes."""

    def snapshot(self, token_ids: list[str], *, moment: str, **context: Any) -> None:
        return None


def build_book_snapshot_logger(data_dir: Path, enabled: bool, *, fetcher: BookFetcher | None = None) -> Any:
    return BookSnapshotLogger(data_dir, fetcher=fetcher) if enabled else NullBookSnapshotLogger()


__all__ = ["BookSnapshotLogger", "NullBookSnapshotLogger", "build_book_snapshot_logger"]
