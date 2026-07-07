from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from polybot.log import log_event

# Reused as-is: these are already parametrized purely by token_id, with no
# single-market assumptions, so they work unmodified for cross-market rotation.
from polybot.iran.executor import DryRunTradingAdapter, Fill, LivePosition, LiveClobTradingAdapter, TradingAdapter  # noqa: F401
from polybot.iran.notifier import Notifier
from polybot.iran.storage import StateStore
from polybot.iran.types import Article

from .config import LocationBotConfig, OutcomeMarket
from .decision import LocationDecision

# Local terminal-state set (deliberately not reusing polybot.iran.storage's
# TERMINAL_STATES, which lacks "ROTATED" and is shared/mutable global state we
# don't want to alter from another package as a side effect).
TERMINAL_STATES = {"EXITED", "ROTATED", "FLIP_INCOMPLETE", "STOPPED"}


class LocationExecutor:
    def __init__(self, config: LocationBotConfig, store: StateStore, notifier: Notifier, adapter: TradingAdapter):
        self.config = config
        self.store = _effective_store(config, store)
        self.notifier = notifier
        self.adapter = adapter

    def execute(self, decision: LocationDecision, article: Article) -> str:
        try:
            return self._execute(decision, article)
        except Exception as exc:
            current = self.store.current()
            self.store.write(
                "EXECUTION_ERROR",
                reason=str(exc),
                previous_state=current.state if current else None,
                decision=_decision_dict(decision),
                article=article.__dict__,
            )
            log_event("location_execution_error", action=decision.action, error=str(exc))
            self.notifier.notify("Location protection execution failed; bot will continue polling", error=str(exc))
            return "EXECUTION_ERROR"

    def _execute(self, decision: LocationDecision, article: Article) -> str:
        if decision.action not in {"TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES"}:
            log_event("location_execution_skip", action=decision.action, reason=decision.reason)
            return "SKIPPED"

        held = self.config.held_outcome()

        if decision.action == "TRIM_YES":
            prior = self.store.marker("TRIMMED")
            current = self.store.current()
            if (current and current.state in {"TRIMMED", "EXITED", "ROTATED"}) or prior is not None:
                self.notifier.notify("YES trim already recorded; no duplicate trim", state=(current.state if current else prior.state))
                return current.state if current else prior.state

        current_for_terminal = self.store.current()
        if current_for_terminal is not None and current_for_terminal.state in TERMINAL_STATES and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no trade", state=current_for_terminal.state)
            return current_for_terminal.state

        if not self.config.execution.sell.enabled:
            self.store.write("STOPPED", reason="sell_disabled", article=article.__dict__)
            self.notifier.notify("Sell disabled; no trade")
            return "STOPPED"

        floor_state = self._time_decay_floor_block(decision, held)
        if floor_state is not None:
            return floor_state

        self.store.write("TRIGGER_DETECTED", decision=_decision_dict(decision), article=article.__dict__)
        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", outcome=held.name)
            cancel_result = self.adapter.cancel_open_orders_for_market(held.condition_id)

        position = self.adapter.query_live_position(held.yes_token_id, held.no_token_id)
        if position.yes_shares <= 0:
            self.store.write("YES_POSITION_UNCONFIRMED", reason="no_live_yes_balance", cancel_result=cancel_result, article=article.__dict__)
            self.notifier.notify("No live YES balance found on held outcome; no trade attempted")
            return "YES_POSITION_UNCONFIRMED"
        if self.config.safety.token_mapping_must_match and (
            position.yes_token_id != held.yes_token_id or position.no_token_id != held.no_token_id
        ):
            self.store.write("STOPPED", reason="token_mapping_mismatch", position=asdict(position))
            self.notifier.notify("Token mapping mismatch on held outcome; no trade")
            return "STOPPED"

        target_shares = min(position.yes_shares, self.config.position.max_yes_shares_to_sell)
        if decision.action == "TRIM_YES":
            fraction = self.config.time_decay.trim_fraction or self.config.execution.sell.trim_fraction
            target_shares = max(0.0, min(target_shares, position.yes_shares * fraction))
        if target_shares <= 0:
            self.store.write("STOPPED", reason="zero_sell_size", article=article.__dict__)
            self.notifier.notify("Sell size is zero; no trade")
            return "STOPPED"

        pre_trade_bid = self.adapter.yes_best_bid(held.yes_token_id)
        pre_trade_ask = self.adapter.yes_best_ask(held.yes_token_id)
        self.store.write(
            "SELLING_YES",
            outcome=held.name,
            shares_to_sell=target_shares,
            live_yes_shares=position.yes_shares,
            live_no_shares=position.no_shares,
            pre_trade_yes_best_bid=pre_trade_bid,
            pre_trade_yes_best_ask=pre_trade_ask,
            sell_min_price=self.config.execution.sell.min_price,
            cancel_result=cancel_result,
        )
        sell_result = self.adapter.sell_yes_fak(held.yes_token_id, target_shares, self.config.execution.sell.min_price)
        sell_fill = self.adapter.verify_fill(sell_result, held.yes_token_id)
        total_sold = sell_fill.filled_shares

        if total_sold < target_shares and self.config.execution.sell.retry_partial_once:
            self.store.write(
                "YES_PARTIAL",
                outcome=held.name,
                filled_shares=total_sold,
                remaining=target_shares - total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
            )
            time.sleep(self.config.execution.sell.retry_delay_seconds)
            retry_result = self.adapter.sell_yes_fak(held.yes_token_id, target_shares - total_sold, self.config.execution.sell.min_price)
            total_sold += self.adapter.verify_fill(retry_result, held.yes_token_id).filled_shares

        if total_sold < target_shares:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="yes_partially_sold",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                article=article.__dict__,
            )
            self.notifier.notify("YES partially sold on held outcome. Manual intervention required.", total_sold=total_sold)
            return "FLIP_INCOMPLETE"

        if decision.action == "TRIM_YES":
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "TRIMMED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.notifier.notify("Trimmed held-outcome YES exposure", outcome=held.label, total_sold=total_sold)
            return "TRIMMED"

        if decision.action == "EXIT_YES_ONLY" or not self.config.execution.buy_rotation.enabled:
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.notifier.notify("Exited held-outcome YES exposure (sell-only)", outcome=held.label, total_sold=total_sold)
            return "EXITED"

        target = self.config.outcome(decision.target_outcome or "")
        if target is None:
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                reason="rotation_target_missing",
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.notifier.notify("Rotation target missing from config; exited without buying", total_sold=total_sold)
            return "EXITED"

        return self._buy_rotation_leg(decision, article, held, target, total_sold, target_shares, sell_fill, pre_trade_bid, pre_trade_ask)

    def _time_decay_floor_block(self, decision: LocationDecision, held: OutcomeMarket) -> str | None:
        """Mirror of the iran executor's TIME_DECAY_PRICE_FLOOR guard.

        Calendar-decay sales must not dump the position below the configured
        best-bid floor. Returns the nonterminal state name when blocked,
        None when the sale may proceed. Only applies to time-decay decisions
        (level "TIME"); news-triggered sells are deliberately not floored.
        """
        if decision.level != "TIME":
            return None
        floor = (
            self.config.time_decay.min_trim_price
            if decision.action == "TRIM_YES"
            else self.config.time_decay.min_exit_price
        )
        if floor <= 0:
            return None
        yes_bid = self.adapter.yes_best_bid(held.yes_token_id)
        if yes_bid is not None and yes_bid >= floor:
            return None
        current = self.store.current()
        self.store.write(
            "TIME_DECAY_PRICE_FLOOR",
            reason="yes_bid_below_time_decay_floor",
            action=decision.action,
            outcome=held.name,
            yes_best_bid=yes_bid,
            price_floor=floor,
        )
        if current is None or current.state != "TIME_DECAY_PRICE_FLOOR":
            self.notifier.notify(
                "Time-decay YES sale skipped; best bid below floor",
                action=decision.action,
                outcome=held.label,
                yes_best_bid=yes_bid,
                price_floor=floor,
            )
        return "TIME_DECAY_PRICE_FLOOR"

    def _estimate_sale_price(self, sell_fill: Fill, pre_trade_bid: float | None) -> float:
        """Best available estimate of the actual per-share proceeds from the
        held-outcome sell, used to cap the rotation buy (see _buy_rotation_leg).

        Tries the fill's raw order response first (adapters that surface a
        fill price there give the most accurate number), falls back to the
        best bid observed immediately before the sell was placed, and only
        falls back to the configured min_price floor -- a worst-case, never
        an average-case -- if neither is available.
        """
        raw = sell_fill.raw
        if isinstance(raw, dict):
            for key in ("avg_price", "average_price", "price", "avgPrice"):
                value = raw.get(key)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        if pre_trade_bid is not None and pre_trade_bid > 0:
            return pre_trade_bid
        return self.config.execution.sell.min_price

    def _buy_rotation_leg(
        self,
        decision: LocationDecision,
        article: Article,
        held: OutcomeMarket,
        target: OutcomeMarket,
        total_sold: float,
        target_shares: float,
        sell_fill: Fill,
        pre_trade_bid: float | None,
        pre_trade_ask: float | None,
    ) -> str:
        cap = self.config.execution.buy_rotation.max_price
        sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
        confirmed_proceeds = total_sold * sale_price
        target_ask = self.adapter.yes_best_ask(target.yes_token_id)
        if target_ask is None or target_ask > cap:
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                reason="rotation_target_above_cap_or_unavailable",
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=confirmed_proceeds,
                configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
                max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
                article=article.__dict__,
            )
            self.notifier.notify(
                "Sold held YES. Rotation buy skipped (target price above cap or unavailable).",
                target=target.label,
                target_best_ask=target_ask,
                cap=cap,
            )
            return "EXITED"

        # Rotation buy is capped by actual confirmed sale proceeds, not blindly
        # by the configured budget -- a partial fill, thin book, or bad price
        # on the sell leg must not let the buy leg overspend relative to what
        # was actually raised (found during 2026-07-06 hardening review).
        usd_budget = min(
            self.config.execution.buy_rotation.usd_budget,
            self.config.position.max_rotation_usd_to_buy,
            confirmed_proceeds,
        )
        min_viable_order_usd = 1.0
        if usd_budget < min_viable_order_usd:
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                reason="insufficient_sale_proceeds_for_rotation_buy",
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                confirmed_proceeds=confirmed_proceeds,
                sale_price_used=sale_price,
                configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
                max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
                article=article.__dict__,
            )
            self.notifier.notify(
                "Sold held YES. Rotation buy skipped (confirmed sale proceeds too small to fund a real order).",
                target=target.label,
                confirmed_proceeds=confirmed_proceeds,
            )
            return "EXITED"

        if self.config.safety.cancel_open_orders_first:
            self.adapter.cancel_open_orders_for_market(target.condition_id)
        self.store.write(
            "BUYING_ROTATION",
            target_outcome=target.name,
            usd_budget=usd_budget,
            cap=cap,
            target_best_ask=target_ask,
            confirmed_proceeds=confirmed_proceeds,
            sale_price_used=sale_price,
            configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
            max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
        )
        buy_result = self.adapter.buy_yes_fak(target.yes_token_id, usd_budget, cap)
        buy_fill = self.adapter.verify_fill(buy_result, target.yes_token_id)
        if buy_fill.filled_shares <= 0:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="rotation_buy_unfilled",
                outcome=held.name,
                total_sold=total_sold,
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                usd_budget=usd_budget,
                confirmed_proceeds=confirmed_proceeds,
                sale_price_used=sale_price,
                article=article.__dict__,
            )
            self.notifier.notify("Sold held YES but rotation buy did not fill. Manual intervention required.", total_sold=total_sold, target=target.label)
            return "FLIP_INCOMPLETE"

        self.store.write(
            "ROTATED",
            from_outcome=held.name,
            to_outcome=target.name,
            total_sold=total_sold,
            target_shares=target_shares,
            pre_trade_yes_best_bid=pre_trade_bid,
            pre_trade_yes_best_ask=pre_trade_ask,
            sell_min_price=self.config.execution.sell.min_price,
            sale_price_used=sale_price,
            confirmed_proceeds=confirmed_proceeds,
            target_best_ask=target_ask,
            target_max_price=cap,
            rotation_usd_budget=usd_budget,
            configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
            max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
            rotation_filled_shares=buy_fill.filled_shares,
            article=article.__dict__,
        )
        self.notifier.notify(
            f"Rotated: sold {held.label}-YES, bought {target.label}-YES",
            total_sold=total_sold,
            rotation_filled_shares=buy_fill.filled_shares,
        )
        return "ROTATED"


def _effective_store(config: LocationBotConfig, store: StateStore) -> StateStore:
    if config.execution.dry_run and store.data_dir.name != "dry_run":
        return StateStore(store.data_dir / "dry_run")
    return store


def _decision_dict(decision: LocationDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "level": decision.level,
        "reason": decision.reason,
        "target_outcome": decision.target_outcome,
        "factors": decision.factors.__dict__ if decision.factors else None,
    }
