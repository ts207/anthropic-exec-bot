from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

from .book import BookCache
from .config import SETTINGS
from .gamma import MarketMeta
from .log import log_event
from .risk import RiskState
from .settlement import SettlementWatcher


@dataclass(frozen=True)
class OrderResult:
    submitted: bool
    accepted: bool
    dry_run: bool
    reason: str | None = None
    order_id: str | None = None
    raw: Any = None


def build_clob_client() -> ClobClient:
    creds = None
    if SETTINGS.clob_api_key and SETTINGS.clob_secret and SETTINGS.clob_passphrase:
        creds = ApiCreds(
            api_key=SETTINGS.clob_api_key,
            api_secret=SETTINGS.clob_secret,
            api_passphrase=SETTINGS.clob_passphrase,
        )
    return ClobClient(
        host=SETTINGS.clob_host,
        chain_id=SETTINGS.chain_id,
        key=SETTINGS.private_key,
        creds=creds,
        signature_type=SETTINGS.signature_type,
        funder=SETTINGS.funder_address,
    )


def prepare(token_ids: list[str], cap: float) -> dict[str, Any]:
    log_event(
        "order_prepare",
        status="not_presigned",
        reason="py-clob-client 0.34.6 create_market_order requires final amount",
        token_ids=token_ids,
        cap=cap,
    )
    return {"presigned": False, "reason": "final amount required"}


def submit_capped_order(
    *,
    market: MarketMeta,
    token_id: str,
    cap_price: float,
    book: BookCache,
    risk: RiskState,
    client: ClobClient | None = None,
    settlement_watcher: SettlementWatcher | None = None,
) -> OrderResult:
    guardrails = SETTINGS.guardrails
    allowed_cap = guardrails.max_entry_price_revisable if market.revisable_rule else guardrails.max_entry_price
    if cap_price > allowed_cap:
        return _skip("price_above_cap", market, token_id, cap_price=cap_price, allowed_cap=allowed_cap)
    if risk.halted:
        return _skip("halted", market, token_id)

    refreshed = _refresh_market(market)
    if refreshed is None:
        return _skip("not_tradeable", market, token_id, reason_detail="market_refresh_failed")
    if not refreshed.tradeable():
        return _skip("not_tradeable", market, token_id, active=refreshed.active, closed=refreshed.closed)
    market_key = refreshed.condition_id or refreshed.market_slug
    if market_key in risk.traded_markets:
        return _skip("already_traded", refreshed, token_id)

    best_ask = book.best_ask(token_id)
    stale = book.staleness(token_id)
    if best_ask is None or stale is None or stale > guardrails.max_book_staleness_seconds:
        try:
            book.rest_snapshot(token_id)
        except Exception as exc:
            return _skip("stale_book", refreshed, token_id, error=str(exc), staleness=stale)
        best_ask = book.best_ask(token_id)
        stale = book.staleness(token_id)
    if best_ask is None or stale is None or stale > guardrails.max_book_staleness_seconds:
        return _skip("stale_book", refreshed, token_id, staleness=stale)
    if best_ask > cap_price:
        return _skip("price_above_cap", refreshed, token_id, best_ask=best_ask, cap_price=cap_price)

    try:
        book.rest_snapshot(token_id)
    except Exception as exc:
        return _skip("stale_book", refreshed, token_id, error=str(exc), staleness=stale, phase="pre_size_resnapshot")
    best_ask = book.best_ask(token_id)
    stale = book.staleness(token_id)
    if best_ask is None or stale is None or stale > guardrails.max_book_staleness_seconds:
        return _skip("stale_book", refreshed, token_id, staleness=stale, phase="pre_size_resnapshot")
    if best_ask > cap_price:
        return _skip("price_above_cap", refreshed, token_id, best_ask=best_ask, cap_price=cap_price, phase="pre_size_resnapshot")

    depth = book.depth_under_cap(token_id, cap_price)
    if depth <= 0:
        return _skip("no_depth", refreshed, token_id, cap_price=cap_price)
    allowed_notional = risk.allowed_notional(market_key)
    if allowed_notional <= 0:
        reason = "daily_limit" if risk.remaining_for_day() <= 0 else "already_traded"
        return _skip(reason, refreshed, token_id, market_remaining=risk.remaining_for_market(market_key), day_remaining=risk.remaining_for_day())
    amount = _round_amount(min(depth, allowed_notional, guardrails.per_order_notional))
    if amount <= 0:
        return _skip("daily_limit", refreshed, token_id)
    rounded_cap = _round_price(cap_price, refreshed.tick_size)

    args = {
        "token_id": token_id,
        "amount": amount,
        "side": BUY,
        "price": rounded_cap,
        "order_type": OrderType.FAK,
    }
    if SETTINGS.dry_run:
        log_event("order_submit", dry_run=True, market_key=market_key, args=args, best_ask=best_ask, depth_under_cap=depth)
        risk.record_dry_run_attempt(market_key)
        return OrderResult(submitted=False, accepted=False, dry_run=True, reason="DRY_RUN")

    if client is None:
        client = build_clob_client()
    risk.reserve_order_attempt(market_key, amount)
    try:
        order = client.create_market_order(
            MarketOrderArgs(**args),
            PartialCreateOrderOptions(tick_size=refreshed.tick_size, neg_risk=refreshed.neg_risk),
        )
        response = client.post_order(order, OrderType.FAK)
    except Exception as exc:
        log_event("order_submit", dry_run=False, accepted=False, error=str(exc), args=args)
        return OrderResult(submitted=True, accepted=False, dry_run=False, reason="submit_error", raw={"error": str(exc)})

    accepted = _accepted(response)
    order_id = _order_id(response)
    log_event("order_submit", dry_run=False, accepted=accepted, order_id=order_id, raw=response, args=args)
    if accepted:
        if settlement_watcher and order_id:
            settlement_watcher.register(order_id, market_key, token_id)
    return OrderResult(submitted=True, accepted=accepted, dry_run=False, order_id=order_id, raw=response)


def _skip(reason: str, market: MarketMeta, token_id: str, **fields: Any) -> OrderResult:
    log_event("order_skip", reason=reason, market_slug=market.market_slug, condition_id=market.condition_id, token_id=token_id, **fields)
    return OrderResult(submitted=False, accepted=False, dry_run=SETTINGS.dry_run, reason=reason)


def _refresh_market(market: MarketMeta) -> MarketMeta | None:
    if not market.event_slug:
        return market
    try:
        markets = [candidate for candidate in select_all_markets(market.event_slug) if candidate.condition_id == market.condition_id]
        if len(markets) == 1:
            return markets[0]
        raise ValueError(f"condition_id match count={len(markets)}")
    except Exception as exc:
        log_event("market_refresh_failed", market_slug=market.market_slug, error=str(exc))
        return None


def select_all_markets(slug: str) -> list[MarketMeta]:
    from .gamma import markets_for_event_slug

    return markets_for_event_slug(slug)


def _round_price(price: float, tick_size: str) -> float:
    tick = float(tick_size)
    if tick <= 0:
        return price
    return math.floor(price / tick) * tick


def _round_amount(amount: float) -> float:
    return math.floor(amount * 100) / 100


def _accepted(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("success") is True:
            return True
        status = str(response.get("status") or "").lower()
        return status in {"matched", "live", "delayed"}
    return False


def _order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    value = response.get("orderID") or response.get("order_id") or response.get("id")
    return str(value) if value else None
