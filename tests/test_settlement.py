from __future__ import annotations

from pathlib import Path

from polybot.risk import RiskState
from polybot.settlement import SettlementWatcher, classify_trade_status


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def get_trades(self, _params):
        return self.responses.pop(0)


def test_classify_matched_is_pending() -> None:
    assert classify_trade_status([{"orderID": "abc", "status": "matched"}], "abc") == "pending"


def test_transaction_hash_without_confirmed_status_is_pending() -> None:
    assert classify_trade_status([{"orderID": "abc", "transactionHash": "0x1"}], "abc") == "pending"


def test_classify_confirmed_trade_by_status() -> None:
    assert classify_trade_status([{"orderID": "abc", "status": "confirmed"}], "abc") == "confirmed"


def test_classify_failed_trade_by_status() -> None:
    assert classify_trade_status([{"orderID": "abc", "status": "failed"}], "abc") == "failed"


def test_classify_pending_when_no_matching_trade() -> None:
    assert classify_trade_status([{"orderID": "other", "status": "confirmed"}], "abc") == "pending"


def test_classify_maker_order_list_match() -> None:
    raw = [{"status": "confirmed", "maker_orders": [{"order_id": "abc"}]}]
    assert classify_trade_status(raw, "abc") == "confirmed"


def test_settlement_watcher_terminal_failure_mutates_risk(tmp_path: Path) -> None:
    state = RiskState(path=tmp_path / "risk.json")
    watcher = SettlementWatcher(FakeClient([[{"orderID": "abc", "status": "failed"}]]), state)
    watcher.register("abc", "market", "token")
    watcher.poll_once()
    assert state.consecutive_failures == 1
    assert state.halted is False


def test_settlement_watcher_kill_switch_threshold(tmp_path: Path) -> None:
    state = RiskState(path=tmp_path / "risk.json")
    watcher = SettlementWatcher(FakeClient([[{"orderID": "a", "status": "failed"}], [{"orderID": "b", "status": "failed"}]]), state)
    watcher.register("a", "market-a", "token")
    watcher.register("b", "market-b", "token")
    watcher.poll_once()
    assert state.halted is True
