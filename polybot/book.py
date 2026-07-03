from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import websocket

from .config import SETTINGS
from .log import log_event


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TokenBook:
    token_id: str
    best_ask: float | None = None
    best_bid: float | None = None
    asks: list[tuple[float, float]] = field(default_factory=list)
    bids: list[tuple[float, float]] = field(default_factory=list)
    updated_mono: float | None = None

    def staleness(self) -> float | None:
        if self.updated_mono is None:
            return None
        return max(0.0, time.monotonic() - self.updated_mono)


class BookCache:
    def __init__(self, token_ids: list[str], clob_host: str = SETTINGS.clob_host):
        self.token_ids = [str(token_id) for token_id in token_ids]
        self.clob_host = clob_host.rstrip("/")
        self.books = {token_id: TokenBook(token_id=token_id) for token_id in self.token_ids}
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def best_ask(self, token_id: str) -> float | None:
        with self._lock:
            return self.books[token_id].best_ask

    def staleness(self, token_id: str) -> float | None:
        with self._lock:
            return self.books[token_id].staleness()

    def depth_under_cap(self, token_id: str, cap_price: float) -> float:
        with self._lock:
            book = self.books[token_id]
            return sum(price * size for price, size in book.asks if price <= cap_price)

    def snapshot_state(self, token_id: str) -> dict[str, Any]:
        with self._lock:
            book = self.books[token_id]
            return {
                "token_id": token_id,
                "best_ask": book.best_ask,
                "best_bid": book.best_bid,
                "asks": book.asks[:20],
                "bids": book.bids[:20],
                "staleness": book.staleness(),
            }

    def rest_snapshot(self, token_id: str) -> None:
        response = requests.get(f"{self.clob_host}/book", params={"token_id": token_id}, timeout=10)
        response.raise_for_status()
        self._apply_book(token_id, response.json())
        log_event("book_snapshot", token_id=token_id, source="rest", book=self.snapshot_state(token_id))

    def start_ws(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=lambda _ws, error: log_event("book_ws_error", error=str(error)),
            on_close=lambda _ws, code, reason: log_event("book_ws_close", code=code, reason=reason),
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def stop_ws(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({"type": "market", "assets_ids": self.token_ids, "custom_feature_enabled": True}))
        log_event("book_ws_subscribe", token_ids=self.token_ids)

    def _on_message(self, _ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = decoded if isinstance(decoded, list) else [decoded]
        for message in messages:
            if isinstance(message, dict):
                self._apply_event(message)

    def _apply_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        if event_type == "book":
            token_id = str(event.get("asset_id") or event.get("token_id") or "")
            if token_id in self.books:
                self._apply_book(token_id, event)
            return
        if event_type == "best_bid_ask":
            token_id = str(event.get("asset_id") or "")
            if token_id in self.books:
                with self._lock:
                    book = self.books[token_id]
                    book.best_ask = _as_float(event.get("best_ask"))
                    book.best_bid = _as_float(event.get("best_bid"))
                    book.updated_mono = time.monotonic()
            return
        if event_type == "price_change":
            changes = event.get("price_changes")
            if not isinstance(changes, list):
                changes = [event]
            for change in changes:
                if not isinstance(change, dict):
                    continue
                token_id = str(change.get("asset_id") or event.get("asset_id") or "")
                if token_id not in self.books:
                    continue
                with self._lock:
                    book = self.books[token_id]
                    ask = _as_float(change.get("best_ask") or event.get("best_ask"))
                    bid = _as_float(change.get("best_bid") or event.get("best_bid"))
                    if ask is not None:
                        book.best_ask = ask
                    if bid is not None:
                        book.best_bid = bid
                    book.updated_mono = time.monotonic()

    def _apply_book(self, token_id: str, payload: dict[str, Any]) -> None:
        asks = _levels(payload.get("asks"))
        bids = _levels(payload.get("bids"))
        with self._lock:
            book = self.books[token_id]
            book.asks = sorted(asks, key=lambda level: level[0])
            book.bids = sorted(bids, key=lambda level: level[0], reverse=True)
            book.best_ask = book.asks[0][0] if book.asks else _as_float(payload.get("best_ask"))
            book.best_bid = book.bids[0][0] if book.bids else _as_float(payload.get("best_bid"))
            book.updated_mono = time.monotonic()


def _levels(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, dict):
            continue
        price = _as_float(level.get("price"))
        size = _as_float(level.get("size"))
        if price is not None and size is not None and size > 0:
            levels.append((price, size))
    return levels
