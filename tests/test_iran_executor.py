from __future__ import annotations

from pathlib import Path
from typing import Any

from polybot.gamma import MarketMeta
from polybot.iran.config import BuyYesConfig, ExecutionConfig, IranBotConfig, MarketConfig, PositionConfig, SafetyConfig, SellNoConfig, TimeDecayConfig
from polybot.iran.decision import Decision
from polybot.iran.executor import DryRunTradingAdapter, Fill, FlipExecutor, LiveClobTradingAdapter, LivePosition, TsClobV2TradingAdapter
from polybot.iran.notifier import Notifier
from polybot.iran.storage import StateStore
from polybot.iran.types import Article


class CapturingNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def notify(self, message: str, **fields: Any) -> None:
        self.messages.append((message, fields))


class FakeAdapter(DryRunTradingAdapter):
    def __init__(
        self,
        *,
        no_shares: float = 10,
        yes_shares: float = 10,
        yes_ask: float = 0.5,
        no_ask: float = 0.5,
        yes_bid: float = 0.5,
        sell_fills: list[float] | None = None,
        buy_fills: list[float] | None = None,
        mismatch: bool = False,
        fail_on: str | None = None,
    ):
        super().__init__(no_shares=no_shares, yes_shares=yes_shares, yes_ask=yes_ask, no_ask=no_ask, yes_bid=yes_bid)
        self.sell_fills = sell_fills or []
        self.buy_fills = buy_fills or []
        self.mismatch = mismatch
        self.fail_on = fail_on
        self.calls: list[str] = []

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        self.calls.append("query_live_position")
        if self.mismatch:
            return LivePosition(yes_token_id="wrong_yes", no_token_id=no_token_id, no_shares=self.no_shares)
        return super().query_live_position(yes_token_id, no_token_id)

    def cancel_open_orders_for_market(self, condition_id: str) -> dict[str, Any]:
        self.calls.append("cancel")
        return super().cancel_open_orders_for_market(condition_id)

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        self.calls.append("sell")
        if self.fail_on == "sell":
            raise RuntimeError("sell failed")
        return {"side": "SELL", "token_id": no_token_id, "shares": shares, "min_price": min_price}

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        self.calls.append("sell_yes")
        return {"side": "SELL", "token_id": yes_token_id, "shares": shares, "min_price": min_price}

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        self.calls.append("buy")
        return super().buy_yes_fak(yes_token_id, usd, max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        self.calls.append("buy_no")
        return super().buy_no_fak(no_token_id, usd, max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("side") == "SELL" and self.sell_fills:
            return Fill(filled_shares=self.sell_fills.pop(0), raw=result)
        if isinstance(result, dict) and result.get("side") == "BUY" and self.buy_fills:
            return Fill(filled_shares=self.buy_fills.pop(0), raw=result)
        return super().verify_fill(result, token_id)


def market() -> MarketMeta:
    return MarketMeta(
        event_slug="iran-event",
        market_slug="july-17",
        condition_id="cond",
        question="Will the next round of US-Iran peace talks happen by July 17?",
        description="rules",
        resolution_source="source",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        yes_token_id="yes",
        no_token_id="no",
        tick_size="0.01",
        neg_risk=False,
        active=True,
        closed=False,
        accepting_orders=True,
        volume=1,
        liquidity=1,
    )


def config(tmp_path: Path, *, dry_run: bool = True, sell_no_enabled: bool = True) -> IranBotConfig:
    return IranBotConfig(
        market=MarketConfig(slug="iran-event"),
        position=PositionConfig(max_yes_usd_to_buy=100),
        execution=ExecutionConfig(
            dry_run=dry_run,
            sell_no=SellNoConfig(enabled=sell_no_enabled, retry_delay_seconds=0),
            buy_yes=BuyYesConfig(max_price_level4a=0.90, max_price_level4b=0.95, usd_budget=100),
        ),
        safety=SafetyConfig(),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )


def yes_config(tmp_path: Path, *, dry_run: bool = True) -> IranBotConfig:
    cfg = config(tmp_path, dry_run=dry_run)
    return IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        position=PositionConfig(max_yes_shares_to_sell=100, max_no_usd_to_buy=25),
        execution=cfg.execution,
        time_decay=cfg.time_decay,
        safety=cfg.safety,
        data_dir=cfg.data_dir,
        logs_dir=cfg.logs_dir,
    )


def yes_time_decay_config(tmp_path: Path, *, min_exit_price: float = 0.0, yes_bid: float = 0.5) -> tuple[IranBotConfig, FakeAdapter]:
    cfg = yes_config(tmp_path)
    return (
        IranBotConfig(
            market=cfg.market,
            position=cfg.position,
            execution=cfg.execution,
            time_decay=TimeDecayConfig(enabled=True, min_exit_price=min_exit_price),
            safety=cfg.safety,
            data_dir=cfg.data_dir,
            logs_dir=cfg.logs_dir,
        ),
        FakeAdapter(yes_shares=25, yes_bid=yes_bid),
    )


def article() -> Article:
    return Article(
        url="https://reuters.com/story",
        domain="reuters.com",
        title="trigger",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="formal senior-level talks scheduled for July 14",
        hash="h",
    )


def decision(action: str = "SELL_NO_CONDITIONAL_BUY_YES") -> Decision:
    return Decision(action, "4A" if action == "SELL_NO_CONDITIONAL_BUY_YES" else "4B", "test")


def yes_decision(action: str = "EXIT_YES_ONLY") -> Decision:
    return Decision(action, "4B", "test")


def test_flipped_when_no_sold_and_yes_under_cap(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_ask=0.50)
    store = StateStore(tmp_path / "data")
    notifier = CapturingNotifier()
    result = FlipExecutor(config(tmp_path), store, notifier, adapter).execute(decision(), article(), market())
    assert result == "FLIPPED"
    assert (tmp_path / "data" / "dry_run" / "FLIPPED.json").exists()
    assert not (tmp_path / "data" / "FLIPPED.json").exists()
    assert adapter.calls == ["cancel", "query_live_position", "sell", "buy"]


def test_yes_above_cap_still_sells_no_and_skips_yes(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_ask=0.91)
    store = StateStore(tmp_path / "data")
    result = FlipExecutor(config(tmp_path), store, CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "NO_SOLD_YES_SKIPPED"
    assert "sell" in adapter.calls
    assert "buy" not in adapter.calls
    assert (tmp_path / "data" / "dry_run" / "NO_SOLD.json").exists()


def test_partial_no_sell_retries_once_then_marks_incomplete(tmp_path: Path) -> None:
    adapter = FakeAdapter(no_shares=10, sell_fills=[3, 2])
    store = StateStore(tmp_path / "data")
    result = FlipExecutor(config(tmp_path), store, CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "FLIP_INCOMPLETE"
    assert adapter.calls.count("sell") == 2
    assert (tmp_path / "data" / "dry_run" / "FLIP_INCOMPLETE.json").exists()


def test_no_no_balance_is_retryable_without_trade(tmp_path: Path) -> None:
    adapter = FakeAdapter(no_shares=0)
    result = FlipExecutor(config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "NO_POSITION_UNCONFIRMED"
    assert (tmp_path / "data" / "dry_run" / "NO_POSITION_UNCONFIRMED.json").exists() is False
    assert (tmp_path / "data" / "dry_run" / "STOPPED.json").exists() is False
    assert "sell" not in adapter.calls


def test_token_mapping_mismatch_stops_without_trade(tmp_path: Path) -> None:
    adapter = FakeAdapter(mismatch=True)
    result = FlipExecutor(config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "STOPPED"
    assert "sell" not in adapter.calls


def test_terminal_state_prevents_duplicate_execution(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    StateStore(tmp_path / "data" / "dry_run").write("FLIPPED", reason="already_done")
    store = StateStore(tmp_path / "data")
    adapter = FakeAdapter()
    result = FlipExecutor(cfg, store, CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "FLIPPED"
    assert adapter.calls == []


def test_zero_yes_buy_fill_marks_incomplete_not_flipped(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_ask=0.50, buy_fills=[0])
    result = FlipExecutor(config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "FLIP_INCOMPLETE"
    assert (tmp_path / "data" / "dry_run" / "FLIP_INCOMPLETE.json").exists()
    assert not (tmp_path / "data" / "dry_run" / "FLIPPED.json").exists()


def test_dry_run_terminal_state_does_not_block_live_intended_state_path(tmp_path: Path) -> None:
    dry_cfg = config(tmp_path, dry_run=True)
    dry_result = FlipExecutor(dry_cfg, StateStore(tmp_path / "data"), CapturingNotifier(), FakeAdapter()).execute(decision(), article(), market())
    assert dry_result == "FLIPPED"

    live_cfg = config(tmp_path, dry_run=False)
    live_adapter = FakeAdapter()
    live_result = FlipExecutor(live_cfg, StateStore(tmp_path / "data"), CapturingNotifier(), live_adapter).execute(decision(), article(), market())
    assert live_result == "FLIPPED"
    assert live_adapter.calls == ["cancel", "query_live_position", "sell", "buy"]
    assert (tmp_path / "data" / "dry_run" / "FLIPPED.json").exists()
    assert (tmp_path / "data" / "FLIPPED.json").exists()


def test_sell_no_disabled_stops_without_sell_or_buy(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    result = FlipExecutor(config(tmp_path, sell_no_enabled=False), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(decision(), article(), market())
    assert result == "STOPPED"
    assert adapter.calls == []


def test_execution_exception_records_nonterminal_error(tmp_path: Path) -> None:
    adapter = FakeAdapter(fail_on="sell")
    notifier = CapturingNotifier()
    result = FlipExecutor(config(tmp_path), StateStore(tmp_path / "data"), notifier, adapter).execute(decision(), article(), market())
    assert result == "EXECUTION_ERROR"
    state = StateStore(tmp_path / "data" / "dry_run").current()
    assert state is not None
    assert state.state == "EXECUTION_ERROR"
    assert state.payload["previous_state"] == "SELLING_NO"
    assert (tmp_path / "data" / "dry_run" / "EXECUTION_ERROR.json").exists() is False
    assert notifier.messages[-1][0] == "Iran protection execution failed; bot will continue polling"


def test_trim_yes_sells_fraction_without_buying_no(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=40)
    result = FlipExecutor(yes_config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(yes_decision("TRIM_YES"), article(), market())
    assert result == "TRIMMED"
    assert adapter.calls == ["cancel", "query_live_position", "sell_yes"]
    state = StateStore(tmp_path / "data" / "dry_run").current()
    assert state is not None
    assert state.payload["total_sold"] == 10


def test_exit_yes_only_sells_yes_without_no_buy(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=25)
    result = FlipExecutor(yes_config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(yes_decision("EXIT_YES_ONLY"), article(), market())
    assert result == "EXITED"
    assert adapter.calls == ["cancel", "query_live_position", "sell_yes"]


def test_exit_yes_optional_buy_no_under_cap(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=25, no_ask=0.40)
    result = FlipExecutor(yes_config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(yes_decision("EXIT_YES_OPTIONAL_BUY_NO"), article(), market())
    assert result == "EXITED"
    assert adapter.calls == ["cancel", "query_live_position", "sell_yes", "buy_no"]


def test_exit_yes_skips_no_hedge_above_cap(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=25, no_ask=0.95)
    result = FlipExecutor(yes_config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(yes_decision("EXIT_YES_OPTIONAL_BUY_NO"), article(), market())
    assert result == "YES_SOLD_NO_SKIPPED"
    assert adapter.calls == ["cancel", "query_live_position", "sell_yes"]


def test_time_decay_exit_skips_when_yes_bid_below_floor(tmp_path: Path) -> None:
    cfg, adapter = yes_time_decay_config(tmp_path, min_exit_price=0.10, yes_bid=0.04)
    result = FlipExecutor(cfg, StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(
        Decision("EXIT_YES_ONLY", "TIME", "time_decay_exit"),
        article(),
        market(),
    )
    assert result == "TIME_DECAY_PRICE_FLOOR"
    assert "sell_yes" not in adapter.calls
    state = StateStore(tmp_path / "data" / "dry_run").current()
    assert state is not None
    assert state.state == "TIME_DECAY_PRICE_FLOOR"
    assert state.payload["yes_best_bid"] == 0.04


class FakeLiveClient:
    def __init__(self) -> None:
        self.balances = {"yes": 3_891_668_180, "no": 0}

    def get_balance_allowance(self, params):
        return {"balance": str(self.balances.get(params.token_id, 0))}

    def cancel_market_orders(self, market: str = "", asset_id: str = ""):
        return {"cancelled": True, "market": market, "asset_id": asset_id}

    def create_market_order(self, order_args, options=None):
        return order_args

    def post_order(self, order, orderType="GTC", post_only=False):
        if order.side == "SELL":
            self.balances[order.token_id] -= int(round(float(order.amount) * 1_000_000))
        else:
            self.balances[order.token_id] += int(round((float(order.amount) / float(order.price)) * 1_000_000))
        return {"success": True, "status": "matched"}

    def get_order_book(self, token_id: str):
        return {"asks": [{"price": "0.42", "size": "100"}, {"price": "0.50", "size": "100"}]}


def test_live_adapter_uses_raw_conditional_balances_for_fill_math() -> None:
    adapter = LiveClobTradingAdapter(FakeLiveClient())
    position = adapter.query_live_position("yes", "no")
    assert position.yes_shares == 3891.66818
    assert position.no_shares == 0

    sell_result = adapter.sell_yes_fak("yes", 10, 0.03)
    assert adapter.verify_fill(sell_result, "yes").filled_shares == 10

    buy_result = adapter.buy_no_fak("no", 21, 0.42)
    assert adapter.verify_fill(buy_result, "no").filled_shares == 50
    assert adapter.no_best_ask("no") == 0.42


def test_ts_clob_v2_adapter_parses_balance_response(monkeypatch) -> None:
    calls = []

    def fake_run(command, env, check, text, capture_output):
        calls.append((command, env))
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"live_position":{"yes_token_id":"yes","no_token_id":"no","yes_shares":12.5,"no_shares":0}}',
                "stderr": "",
            },
        )()

    monkeypatch.setattr("polybot.iran.executor.subprocess.run", fake_run)

    position = TsClobV2TradingAdapter().query_live_position("yes", "no")

    assert position.yes_shares == 12.5
    assert position.no_shares == 0
    assert "POLYBOT_TS_BRIDGE_ALLOW_POST" not in calls[0][1]


def test_ts_clob_v2_adapter_sets_post_flag_for_fak(monkeypatch) -> None:
    calls = []

    def fake_run(command, env, check, text, capture_output):
        calls.append((command, env))
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"token_id":"yes","filled_shares":5,"side":"SELL"}',
                "stderr": "",
            },
        )()

    monkeypatch.setattr("polybot.iran.executor.subprocess.run", fake_run)

    result = TsClobV2TradingAdapter(tick_size="0.001", neg_risk=True).sell_yes_fak("yes", 5, 0.03)

    assert result["filled_shares"] == 5
    command, env = calls[0]
    assert env["POLYBOT_TS_BRIDGE_ALLOW_POST"] == "1"
    assert "--tick-size" in command
    assert command[command.index("--tick-size") + 1] == "0.001"
    assert "--neg-risk" in command
    assert command[command.index("--neg-risk") + 1] == "true"


def test_trim_not_repeated_after_trimmed_state_overwritten(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=40)
    store = StateStore(tmp_path / "data")
    executor = FlipExecutor(yes_config(tmp_path), store, CapturingNotifier(), adapter)
    assert executor.execute(yes_decision("TRIM_YES"), article(), market()) == "TRIMMED"
    # A transient state overwrites state.json (e.g. a scheduled-hold signal article).
    effective = StateStore(tmp_path / "data" / "dry_run")
    effective.write("YES_SCHEDULED_HOLD_SIGNAL", reason="senior_round_scheduled_hold_not_resolved")
    calls_before = list(adapter.calls)
    assert executor.execute(yes_decision("TRIM_YES"), article(), market()) == "TRIMMED"
    assert adapter.calls == calls_before  # no second sell


def test_trimmed_marker_written(tmp_path: Path) -> None:
    adapter = FakeAdapter(yes_shares=40)
    FlipExecutor(yes_config(tmp_path), StateStore(tmp_path / "data"), CapturingNotifier(), adapter).execute(yes_decision("TRIM_YES"), article(), market())
    assert (tmp_path / "data" / "dry_run" / "TRIMMED.json").exists()
