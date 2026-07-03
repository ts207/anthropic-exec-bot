from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from py_clob_client.clob_types import TradeParams

from .log import log_event
from .risk import RiskState


@dataclass(frozen=True)
class WatchedOrder:
    order_id: str
    market_key: str
    token_id: str
    submitted_mono: float


class SettlementWatcher:
    def __init__(self, client: Any, risk: RiskState, poll_seconds: float = 2.0, timeout_seconds: float = 120.0):
        self.client = client
        self.risk = risk
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._orders: dict[str, WatchedOrder] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def register(self, order_id: str, market_key: str, token_id: str) -> None:
        with self._lock:
            self._orders[order_id] = WatchedOrder(order_id, market_key, token_id, time.monotonic())
        log_event("settlement_watch_registered", order_id=order_id, market_key=market_key, token_id=token_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_seconds)

    def poll_once(self) -> None:
        with self._lock:
            orders = list(self._orders.values())
        for order in orders:
            status, raw = self._status(order)
            if status == "pending":
                if time.monotonic() - order.submitted_mono > self.timeout_seconds:
                    self._finish(order, "timeout", raw)
                continue
            self._finish(order, status, raw)

    def _status(self, order: WatchedOrder) -> tuple[str, Any]:
        try:
            trades = self.client.get_trades(TradeParams(asset_id=order.token_id))
        except Exception as exc:
            log_event("settlement_check", order_id=order.order_id, status="poll_error", error=str(exc))
            return "pending", {"error": str(exc)}
        status = classify_trade_status(trades, order.order_id)
        log_event("settlement_check", order_id=order.order_id, status=status, raw=trades)
        return status, trades

    def _finish(self, order: WatchedOrder, status: str, raw: Any) -> None:
        with self._lock:
            self._orders.pop(order.order_id, None)
        if status == "confirmed":
            self.risk.record_settlement_success(order.market_key)
        else:
            self.risk.record_settlement_failure(order.market_key, status)
        log_event("settlement_terminal", order_id=order.order_id, market_key=order.market_key, status=status, raw=raw)


def classify_trade_status(raw: Any, order_id: str) -> str:
    rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        return "pending"
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifiers = {str(row.get(key)) for key in ("orderID", "order_id", "id", "taker_order_id")}
        maker_orders = row.get("maker_orders")
        if isinstance(maker_orders, list):
            for maker_order in maker_orders:
                if isinstance(maker_order, dict):
                    identifiers.update(
                        str(maker_order.get(key))
                        for key in ("order_id", "orderID", "id")
                        if maker_order.get(key) is not None
                    )
        if order_id not in identifiers and order_id != "":
            continue
        status = str(row.get("status") or row.get("state") or "").lower()
        if status in {"failed", "reverted", "failure"}:
            return "failed"
        if status in {"mined", "confirmed", "success", "settled"}:
            return "confirmed"
    return "pending"
