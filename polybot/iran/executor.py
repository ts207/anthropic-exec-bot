from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from polybot.core import execution as _execution
from polybot.core.execution import DryRunTradingAdapter, Fill, LiveClobTradingAdapter, LivePosition, TradingAdapter, TsClobV2TradingAdapter, TsPolymarketBetaTradingAdapter
from polybot.gamma import MarketMeta
from polybot.log import log_event

from .config import IranBotConfig
from .decision import Decision
from .notifier import Notifier
from .storage import StateStore
from .types import Article

subprocess = _execution.subprocess


class FlipExecutor:
    def __init__(self, config: IranBotConfig, store: StateStore, notifier: Notifier, adapter: TradingAdapter):
        self.config = config
        self.store = _effective_store(config, store)
        self.notifier = notifier
        self.adapter = adapter

    def execute(self, decision: Decision, article: Article, market: MarketMeta) -> str:
        try:
            return self._execute(decision, article, market)
        except Exception as exc:
            current = self.store.current()
            self.store.write(
                "EXECUTION_ERROR",
                reason=str(exc),
                previous_state=current.state if current else None,
                previous_payload=current.payload if current else None,
                decision=decision.__dict__,
                article=article.__dict__,
            )
            log_event("iran_execution_error", action=decision.action, error=str(exc))
            self.notifier.notify("Iran protection execution failed; bot will continue polling", error=str(exc))
            return "EXECUTION_ERROR"

    def _execute(self, decision: Decision, article: Article, market: MarketMeta) -> str:
        if decision.action in {"TRIM_YES", "EXIT_YES_ONLY", "EXIT_YES_OPTIONAL_BUY_NO"}:
            return self._execute_yes_protection(decision, article, market)

        if decision.action not in {"SELL_NO_CONDITIONAL_BUY_YES", "SELL_NO_BUY_YES"}:
            log_event("iran_execution_skip", action=decision.action, reason=decision.reason)
            return "SKIPPED"

        terminal = self.store.terminal_state()
        if terminal is not None and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no trade", state=terminal.state)
            return terminal.state

        self.store.write("TRIGGER_DETECTED", decision=decision.__dict__, article=article.__dict__)
        if not self.config.execution.sell_no.enabled:
            self.store.write("STOPPED", reason="sell_no_disabled", article=article.__dict__)
            self.notifier.notify("Sell NO disabled; no trade")
            return "STOPPED"

        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", held_side="NO", reason="before_position_query")
            cancel_result = self.adapter.cancel_open_orders_for_market(market.condition_id)

        position = self.adapter.query_live_position(market.yes_token_id, market.no_token_id)
        if position.no_shares <= 0:
            self.store.write(
                "NO_POSITION_UNCONFIRMED",
                reason="no_live_no_balance",
                cancel_result=cancel_result,
                article=article.__dict__,
            )
            self.notifier.notify("No live NO balance found; no trade attempted")
            return "NO_POSITION_UNCONFIRMED"

        if not self._token_mapping_ok(position, market):
            self.store.write("STOPPED", reason="token_mapping_mismatch", position=asdict(position))
            self.notifier.notify("Token mapping mismatch; no trade")
            return "STOPPED"

        shares_to_sell = min(position.no_shares, self.config.position.max_no_shares_to_sell)
        if not self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", shares_to_sell=shares_to_sell)
            cancel_result = self.adapter.cancel_open_orders_for_market(market.condition_id)

        self.store.write("SELLING_NO", shares_to_sell=shares_to_sell, cancel_result=cancel_result)
        sell_result = self.adapter.sell_no_fak(market.no_token_id, shares_to_sell, self.config.execution.sell_no.min_price)
        sell_fill = self.adapter.verify_fill(sell_result, market.no_token_id)
        total_sold = sell_fill.filled_shares
        retry_result = None

        if total_sold < shares_to_sell and self.config.execution.sell_no.retry_partial_once:
            self.store.write("NO_PARTIAL", filled_shares=total_sold, remaining=shares_to_sell - total_sold)
            time.sleep(self.config.execution.sell_no.retry_delay_seconds)
            retry_result = self.adapter.sell_no_fak(
                market.no_token_id,
                shares_to_sell - total_sold,
                self.config.execution.sell_no.min_price,
            )
            retry_fill = self.adapter.verify_fill(retry_result, market.no_token_id)
            total_sold += retry_fill.filled_shares

        if total_sold < shares_to_sell:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="no_partially_sold",
                total_sold=total_sold,
                target_shares=shares_to_sell,
                sell_result=sell_result,
                retry_result=retry_result,
                article=article.__dict__,
            )
            self.notifier.notify("NO partially sold. Manual intervention required.", total_sold=total_sold)
            return "FLIP_INCOMPLETE"

        self.store.write("NO_SOLD", total_sold=total_sold, sell_result=sell_result)
        cap = self.config.execution.buy_yes.max_price_level4b if decision.action == "SELL_NO_BUY_YES" else self.config.execution.buy_yes.max_price_level4a
        yes_ask = self.adapter.yes_best_ask(market.yes_token_id)
        if not self.config.execution.buy_yes.enabled or yes_ask is None or yes_ask > cap:
            self.store.write(
                "NO_SOLD_YES_SKIPPED",
                reason="yes_above_cap_or_disabled",
                yes_best_ask=yes_ask,
                cap=cap,
                total_sold=total_sold,
                article=article.__dict__,
            )
            self.notifier.notify("Sold NO. YES skipped due to cap or disabled.", yes_best_ask=yes_ask, cap=cap)
            return "NO_SOLD_YES_SKIPPED"

        self.store.write("BUYING_YES", yes_best_ask=yes_ask, cap=cap)
        buy_result = self.adapter.buy_yes_fak(
            market.yes_token_id,
            min(self.config.execution.buy_yes.usd_budget, self.config.position.max_yes_usd_to_buy),
            cap,
        )
        buy_fill = self.adapter.verify_fill(buy_result, market.yes_token_id)
        if buy_fill.filled_shares <= 0:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="yes_buy_unfilled",
                total_sold=total_sold,
                yes_filled_shares=buy_fill.filled_shares,
                sell_result=sell_result,
                buy_result=buy_result,
                article=article.__dict__,
            )
            self.notifier.notify("NO sold but YES buy did not fill. Manual intervention required.", total_sold=total_sold)
            return "FLIP_INCOMPLETE"

        self.store.write(
            "FLIPPED",
            total_sold=total_sold,
            yes_filled_shares=buy_fill.filled_shares,
            sell_result=sell_result,
            buy_result=buy_result,
            article=article.__dict__,
        )
        self.notifier.notify("Sold NO and bought YES", total_sold=total_sold, yes_filled_shares=buy_fill.filled_shares)
        return "FLIPPED"

    def _token_mapping_ok(self, position: LivePosition, market: MarketMeta) -> bool:
        if not self.config.safety.token_mapping_must_match:
            return True
        if position.yes_token_id != market.yes_token_id or position.no_token_id != market.no_token_id:
            return False
        if self.config.position.expected_yes_token_id and self.config.position.expected_yes_token_id != market.yes_token_id:
            return False
        if self.config.position.expected_no_token_id and self.config.position.expected_no_token_id != market.no_token_id:
            return False
        return True

    def _execute_yes_protection(self, decision: Decision, article: Article, market: MarketMeta) -> str:
        current = self.store.current()
        if decision.action == "TRIM_YES":
            # The TRIMMED marker survives state.json being overwritten by transient
            # states (hold signals, price-floor skips); without it the 30s poll loop
            # could trim again.
            prior = None
            if current and current.state in {"TRIMMED", "EXITED", "YES_SOLD_NO_SKIPPED"}:
                prior = current
            else:
                prior = self.store.marker("TRIMMED")
            if prior is not None:
                self.notifier.notify("YES trim already recorded; no duplicate trim", state=prior.state)
                return prior.state

        terminal = self.store.terminal_state()
        if terminal is not None and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no trade", state=terminal.state)
            return terminal.state

        self.store.write("TRIGGER_DETECTED", decision=decision.__dict__, article=article.__dict__)
        if not self.config.execution.sell_yes.enabled:
            self.store.write("STOPPED", reason="sell_yes_disabled", article=article.__dict__)
            self.notifier.notify("Sell YES disabled; no trade")
            return "STOPPED"

        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", held_side="YES", reason="before_position_query")
            cancel_result = self.adapter.cancel_open_orders_for_market(market.condition_id)

        position = self.adapter.query_live_position(market.yes_token_id, market.no_token_id)
        if position.yes_shares <= 0:
            self.store.write(
                "YES_POSITION_UNCONFIRMED",
                reason="no_live_yes_balance",
                cancel_result=cancel_result,
                article=article.__dict__,
            )
            self.notifier.notify("No live YES balance found; no trade attempted")
            return "YES_POSITION_UNCONFIRMED"
        if not self._token_mapping_ok(position, market):
            self.store.write("STOPPED", reason="token_mapping_mismatch", position=asdict(position))
            self.notifier.notify("Token mapping mismatch; no trade")
            return "STOPPED"

        target_shares = min(position.yes_shares, self.config.position.max_yes_shares_to_sell)
        if decision.action == "TRIM_YES":
            fraction = self.config.time_decay.trim_fraction or self.config.execution.sell_yes.trim_fraction
            target_shares = max(0.0, min(target_shares, position.yes_shares * fraction))
        if target_shares <= 0:
            self.store.write("STOPPED", reason="zero_yes_sell_size", article=article.__dict__)
            self.notifier.notify("YES sell size is zero; no trade")
            return "STOPPED"

        price_floor = _time_decay_price_floor(self.config, decision.action, decision.level)
        if price_floor > 0:
            yes_bid = self.adapter.yes_best_bid(market.yes_token_id)
            if yes_bid is None or yes_bid < price_floor:
                current = self.store.current()
                self.store.write(
                    "TIME_DECAY_PRICE_FLOOR",
                    reason="yes_bid_below_time_decay_floor",
                    action=decision.action,
                    yes_best_bid=yes_bid,
                    price_floor=price_floor,
                    article=article.__dict__,
                )
                if current is None or current.state != "TIME_DECAY_PRICE_FLOOR":
                    self.notifier.notify(
                        "Time-decay YES sale skipped; best bid below floor",
                        action=decision.action,
                        yes_best_bid=yes_bid,
                        price_floor=price_floor,
                    )
                return "TIME_DECAY_PRICE_FLOOR"

        if not self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", shares_to_sell=target_shares, held_side="YES")
            cancel_result = self.adapter.cancel_open_orders_for_market(market.condition_id)
        self.store.write("SELLING_YES", shares_to_sell=target_shares, cancel_result=cancel_result)
        sell_result = self.adapter.sell_yes_fak(market.yes_token_id, target_shares, self.config.execution.sell_yes.min_price)
        sell_fill = self.adapter.verify_fill(sell_result, market.yes_token_id)
        total_sold = sell_fill.filled_shares
        retry_result = None

        if total_sold < target_shares and self.config.execution.sell_yes.retry_partial_once:
            self.store.write("YES_PARTIAL", filled_shares=total_sold, remaining=target_shares - total_sold)
            time.sleep(self.config.execution.sell_yes.retry_delay_seconds)
            retry_result = self.adapter.sell_yes_fak(
                market.yes_token_id,
                target_shares - total_sold,
                self.config.execution.sell_yes.min_price,
            )
            retry_fill = self.adapter.verify_fill(retry_result, market.yes_token_id)
            total_sold += retry_fill.filled_shares

        if total_sold < target_shares:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="yes_partially_sold",
                total_sold=total_sold,
                target_shares=target_shares,
                sell_result=sell_result,
                retry_result=retry_result,
                article=article.__dict__,
            )
            self.notifier.notify("YES partially sold. Manual intervention required.", total_sold=total_sold)
            return "FLIP_INCOMPLETE"

        if decision.action == "TRIM_YES":
            self.store.write("TRIMMED", total_sold=total_sold, sell_result=sell_result, article=article.__dict__)
            self.notifier.notify("Trimmed YES exposure", total_sold=total_sold)
            return "TRIMMED"

        if decision.action == "EXIT_YES_ONLY" or not self.config.execution.buy_no.enabled:
            self.store.write("EXITED", total_sold=total_sold, sell_result=sell_result, article=article.__dict__)
            self.notifier.notify("Exited YES exposure", total_sold=total_sold)
            return "EXITED"

        cap = self.config.execution.buy_no.max_price_exit
        no_ask = self.adapter.no_best_ask(market.no_token_id)
        if no_ask is None or no_ask > cap:
            self.store.write(
                "YES_SOLD_NO_SKIPPED",
                reason="no_above_cap_or_unavailable",
                no_best_ask=no_ask,
                cap=cap,
                total_sold=total_sold,
                article=article.__dict__,
            )
            self.notifier.notify("Sold YES. NO hedge skipped due to cap or unavailable.", no_best_ask=no_ask, cap=cap)
            return "YES_SOLD_NO_SKIPPED"

        self.store.write("BUYING_NO", no_best_ask=no_ask, cap=cap)
        buy_result = self.adapter.buy_no_fak(
            market.no_token_id,
            min(self.config.execution.buy_no.usd_budget, self.config.position.max_no_usd_to_buy),
            cap,
        )
        buy_fill = self.adapter.verify_fill(buy_result, market.no_token_id)
        self.store.write(
            "EXITED",
            total_sold=total_sold,
            no_filled_shares=buy_fill.filled_shares,
            sell_result=sell_result,
            buy_result=buy_result,
            article=article.__dict__,
        )
        self.notifier.notify("Sold YES and optionally hedged with NO", total_sold=total_sold, no_filled_shares=buy_fill.filled_shares)
        return "EXITED"


def _effective_store(config: IranBotConfig, store: StateStore) -> StateStore:
    if config.execution.dry_run and store.data_dir.name != "dry_run":
        return StateStore(store.data_dir / "dry_run")
    return store


def _time_decay_price_floor(config: IranBotConfig, action: str, level: str) -> float:
    if level != "TIME":
        return 0.0
    if action == "TRIM_YES":
        return config.time_decay.min_trim_price
    if action == "EXIT_YES_ONLY":
        return config.time_decay.min_exit_price
    return 0.0
