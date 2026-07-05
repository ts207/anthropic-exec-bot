from __future__ import annotations

import time
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from polybot.gamma import MarketMeta
from polybot.log import log_event

from .config import IranBotConfig
from .decision import Decision
from .notifier import Notifier
from .storage import StateStore
from .types import Article


@dataclass(frozen=True)
class LivePosition:
    yes_token_id: str
    no_token_id: str
    no_shares: float
    yes_shares: float = 0.0


@dataclass(frozen=True)
class Fill:
    filled_shares: float
    raw: Any = None


class TradingAdapter(Protocol):
    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        ...

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        ...

    def open_orders_for_market(self, condition_id: str) -> Any:
        ...

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> Any:
        ...

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> Any:
        ...

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> Any:
        ...

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> Any:
        ...

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        ...

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        ...

    def no_best_ask(self, no_token_id: str) -> float | None:
        ...

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        ...


class DryRunTradingAdapter:
    def __init__(
        self,
        no_shares: float = 1.0,
        yes_shares: float = 1.0,
        yes_ask: float = 0.50,
        no_ask: float = 0.50,
        yes_bid: float = 0.50,
    ):
        self.no_shares = no_shares
        self.yes_shares = yes_shares
        self.yes_ask_value = yes_ask
        self.no_ask_value = no_ask
        self.yes_bid_value = yes_bid

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=self.no_shares, yes_shares=self.yes_shares)

    def cancel_open_orders_for_market(self, condition_id: str) -> dict[str, Any]:
        return {"dry_run": True, "condition_id": condition_id}

    def open_orders_for_market(self, condition_id: str) -> list[Any]:
        return []

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "SELL", "token_id": no_token_id, "shares": shares, "min_price": min_price}

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "SELL", "token_id": yes_token_id, "shares": shares, "min_price": min_price}

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "BUY", "token_id": yes_token_id, "usd": usd, "max_price": max_price}

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "BUY", "token_id": no_token_id, "usd": usd, "max_price": max_price}

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("side") == "SELL":
            return Fill(filled_shares=float(result["shares"]), raw=result)
        if isinstance(result, dict) and result.get("side") == "BUY":
            return Fill(filled_shares=float(result["usd"]) / float(result["max_price"]), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self.yes_ask_value

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self.no_ask_value

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self.yes_bid_value


class LiveClobTradingAdapter:
    CONDITIONAL_TOKEN_DECIMALS = Decimal("1000000")
    BALANCE_POLL_ATTEMPTS = 20
    BALANCE_POLL_INTERVAL_SECONDS = 0.5

    def __init__(self, client: Any | None = None):
        if client is None:
            from polybot.exec_engine import build_clob_client

            client = build_clob_client()
        self.client = client

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_shares=self._conditional_balance(yes_token_id),
            no_shares=self._conditional_balance(no_token_id),
        )

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        return self.client.cancel_market_orders(market=condition_id)

    def open_orders_for_market(self, condition_id: str) -> Any:
        from py_clob_client.clob_types import OpenOrderParams

        return self.client.get_orders(OpenOrderParams(market=condition_id))

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=no_token_id, amount=shares, side="SELL", price=min_price)

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=yes_token_id, amount=shares, side="SELL", price=min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=yes_token_id, amount=usd, side="BUY", price=max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=no_token_id, amount=usd, side="BUY", price=max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("token_id") == token_id:
            return Fill(filled_shares=float(result.get("filled_shares") or 0.0), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._best_ask(yes_token_id)

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self._best_ask(no_token_id)

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._best_bid(yes_token_id)

    def _post_fak(self, *, token_id: str, amount: float, side: str, price: float) -> dict[str, Any]:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        before = self._conditional_balance(token_id)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY if side == "BUY" else SELL,
            price=price,
            order_type=OrderType.FAK,
        )
        # The SDK can infer tick size and neg-risk details from the live market book.
        order = self.client.create_market_order(args, PartialCreateOrderOptions())
        response = self.client.post_order(order, OrderType.FAK)
        response_fill = _extract_fill_from_response(response)
        after = self._poll_balance_change(token_id, before, side)
        balance_fill = max(0.0, after - before) if side == "BUY" else max(0.0, before - after)
        filled = response_fill if response_fill is not None else balance_fill
        return {
            "live": True,
            "side": side,
            "token_id": token_id,
            "amount": amount,
            "price": price,
            "balance_before": before,
            "balance_after": after,
            "filled_shares": filled,
            "response": response,
        }

    def _poll_balance_change(self, token_id: str, before: float, side: str) -> float:
        latest = before
        for _ in range(self.BALANCE_POLL_ATTEMPTS):
            latest = self._conditional_balance(token_id)
            if side == "BUY" and latest > before:
                return latest
            if side == "SELL" and latest < before:
                return latest
            time.sleep(self.BALANCE_POLL_INTERVAL_SECONDS)
        return latest

    def _conditional_balance(self, token_id: str) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        raw = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return _raw_conditional_balance_to_shares(_extract_first(raw, ("balance", "available", "available_balance")))

    def _best_ask(self, token_id: str) -> float | None:
        book = self.client.get_order_book(token_id)
        asks = _extract_levels(book, "asks")
        prices = [_as_float(_extract_first(level, ("price",))) for level in asks]
        prices = [price for price in prices if price is not None]
        return min(prices) if prices else None

    def _best_bid(self, token_id: str) -> float | None:
        book = self.client.get_order_book(token_id)
        bids = _extract_levels(book, "bids")
        prices = [_as_float(_extract_first(level, ("price",))) for level in bids]
        prices = [price for price in prices if price is not None]
        return max(prices) if prices else None


class TsClobV2TradingAdapter:
    bridge_script = "src/clobV2Bridge.ts"
    bridge_name = "clob-v2 bridge"

    def __init__(self, *, tick_size: str = "0.01", neg_risk: bool = False) -> None:
        self.tick_size = tick_size
        self.neg_risk = neg_risk

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        raw = self._run_bridge(
            "balance",
            {
                "yes-token-id": yes_token_id,
                "no-token-id": no_token_id,
            },
        )
        position = raw.get("live_position") if isinstance(raw, dict) else None
        if not isinstance(position, dict):
            raise RuntimeError("clob-v2 bridge balance response missing live_position")
        return LivePosition(
            yes_token_id=str(position.get("yes_token_id") or yes_token_id),
            no_token_id=str(position.get("no_token_id") or no_token_id),
            yes_shares=float(position.get("yes_shares") or 0.0),
            no_shares=float(position.get("no_shares") or 0.0),
        )

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        return self._run_bridge("cancel-market-orders", {"condition-id": condition_id})

    def open_orders_for_market(self, condition_id: str) -> Any:
        raw = self._run_bridge("open-orders", {"condition-id": condition_id})
        return raw.get("open_orders", []) if isinstance(raw, dict) else []

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> Any:
        return self._post_fak(token_id=no_token_id, amount=shares, side="SELL", price=min_price)

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> Any:
        return self._post_fak(token_id=yes_token_id, amount=shares, side="SELL", price=min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> Any:
        return self._post_fak(token_id=yes_token_id, amount=usd, side="BUY", price=max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> Any:
        return self._post_fak(token_id=no_token_id, amount=usd, side="BUY", price=max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("token_id") == token_id:
            return Fill(filled_shares=float(result.get("filled_shares") or 0.0), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._best(yes_token_id, "best_ask")

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self._best(no_token_id, "best_ask")

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._best(yes_token_id, "best_bid")

    def _best(self, token_id: str, field: str) -> float | None:
        raw = self._run_bridge("book", {"token-id": token_id})
        book = raw.get("book") if isinstance(raw, dict) else None
        if not isinstance(book, dict):
            return None
        return _as_float(book.get(field))

    def _post_fak(self, *, token_id: str, amount: float, side: str, price: float) -> dict[str, Any]:
        raw = self._run_bridge(
            "fak",
            {
                "token-id": token_id,
                "side": side,
                "amount": str(amount),
                "price": str(price),
                "tick-size": self.tick_size,
                "neg-risk": "true" if self.neg_risk else "false",
            },
        )
        if not isinstance(raw, dict):
            raise RuntimeError("clob-v2 bridge FAK response was not an object")
        return raw

    def _run_bridge(self, action: str, args: dict[str, str]) -> Any:
        env = dict(os.environ)
        env.setdefault("TMPDIR", "/tmp")
        node24_bin = "/home/tstuv/.nvm/versions/node/v24.16.0/bin"
        if Path(node24_bin).exists():
            env["PATH"] = f"{node24_bin}:{env.get('PATH', '')}"
        if action in {"fak", "cancel-market-orders"}:
            env["POLYBOT_TS_BRIDGE_ALLOW_POST"] = "1"
        command = ["./node_modules/.bin/tsx", self.bridge_script, "--action", action]
        for key, value in args.items():
            command.extend([f"--{key}", str(value)])
        completed = subprocess.run(command, env=env, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RuntimeError(f"{self.bridge_name} {action} failed: {completed.stderr.strip() or completed.stdout.strip()}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.bridge_name} {action} returned non-JSON output: {completed.stdout[:500]}") from exc


class TsPolymarketBetaTradingAdapter(TsClobV2TradingAdapter):
    bridge_script = "src/polymarketBetaBridge.ts"
    bridge_name = "polymarket beta bridge"


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


def _extract_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
    for key in keys:
        if hasattr(value, key):
            return getattr(value, key)
    return None


def _extract_levels(value: Any, key: str) -> list[Any]:
    levels = _extract_first(value, (key,))
    if isinstance(levels, list):
        return levels
    return []


def _extract_fill_from_response(value: Any) -> float | None:
    direct = _as_float(
        _extract_first(
            value,
            (
                "filled_shares",
                "filledShares",
                "filled_size",
                "filledSize",
                "matched_size",
                "matchedSize",
                "size_matched",
                "sizeMatched",
            ),
        )
    )
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("order", "data", "result"):
            nested = value.get(key)
            if nested is not None and nested is not value:
                parsed = _extract_fill_from_response(nested)
                if parsed is not None:
                    return parsed
    return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_conditional_balance_to_shares(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0.0
    return float(parsed / LiveClobTradingAdapter.CONDITIONAL_TOKEN_DECIMALS)
