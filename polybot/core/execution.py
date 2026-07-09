from __future__ import annotations

import time
import json
import os
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol


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


__all__ = [
    "DryRunTradingAdapter",
    "Fill",
    "LiveClobTradingAdapter",
    "LivePosition",
    "TradingAdapter",
    "TsClobV2TradingAdapter",
    "TsPolymarketBetaTradingAdapter",
]
