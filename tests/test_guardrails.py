from __future__ import annotations

from pathlib import Path

from polybot.gamma import MarketMeta
from polybot.risk import RiskState, _today_key
from polybot.exec_engine import submit_capped_order


class FakeBook:
    def __init__(self, best_ask: float | None = 0.5, depth: float = 10.0, stale: float | None = 0.0):
        self._best_ask = best_ask
        self._depth = depth
        self._stale = stale
        self.snapshots = 0

    def best_ask(self, _token_id: str) -> float | None:
        return self._best_ask

    def staleness(self, _token_id: str) -> float | None:
        return self._stale

    def depth_under_cap(self, _token_id: str, _cap: float) -> float:
        return self._depth

    def rest_snapshot(self, _token_id: str) -> None:
        self.snapshots += 1
        self._stale = 0.0

    def snapshot_state(self, _token_id: str) -> dict:
        return {}


def market(**overrides) -> MarketMeta:
    data = dict(
        event_slug="",
        market_slug="m",
        condition_id="c",
        question="Will the highest temperature be 24°C on July 3?",
        description="contains the highest temperature; whole degrees",
        resolution_source="https://example.test",
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
        revisable_rule=False,
    )
    data.update(overrides)
    return MarketMeta(**data)


def risk(tmp_path: Path) -> RiskState:
    return RiskState(path=tmp_path / "risk.json")


def test_cap_rejection_for_revisable_market(tmp_path: Path) -> None:
    result = submit_capped_order(
        market=market(revisable_rule=True),
        token_id="yes",
        cap_price=0.90,
        book=FakeBook(),
        risk=risk(tmp_path),
    )
    assert result.reason == "price_above_cap"


def test_notional_exhaustion_skips(tmp_path: Path) -> None:
    state = risk(tmp_path)
    state.per_day_spent[_today_key()] = state.guardrails.per_day_notional
    result = submit_capped_order(
        market=market(),
        token_id="yes",
        cap_price=0.50,
        book=FakeBook(),
        risk=state,
    )
    assert result.reason == "daily_limit"


def test_kill_switch_threshold_halts(tmp_path: Path) -> None:
    state = risk(tmp_path)
    state.record_settlement_failure("a", "failed")
    assert state.halted is False
    state.record_settlement_failure("b", "timeout")
    assert state.halted is True


def test_single_shot_skips_already_traded(tmp_path: Path) -> None:
    state = risk(tmp_path)
    state.traded_markets.add("c")
    result = submit_capped_order(
        market=market(),
        token_id="yes",
        cap_price=0.50,
        book=FakeBook(),
        risk=state,
    )
    assert result.reason == "already_traded"


def test_dry_run_does_not_submit_live(tmp_path: Path, monkeypatch) -> None:
    def fail_build():
        raise AssertionError("dry-run must not build a live client")

    monkeypatch.setattr("polybot.exec_engine.build_clob_client", fail_build)
    result = submit_capped_order(
        market=market(),
        token_id="yes",
        cap_price=0.50,
        book=FakeBook(best_ask=0.49, depth=12),
        risk=risk(tmp_path),
    )
    assert result.dry_run is True
    assert result.submitted is False
    assert result.reason == "DRY_RUN"


def test_dry_run_marks_market_in_memory_for_single_shot(tmp_path: Path) -> None:
    state = risk(tmp_path)
    first = submit_capped_order(
        market=market(),
        token_id="yes",
        cap_price=0.50,
        book=FakeBook(best_ask=0.49, depth=12),
        risk=state,
    )
    second = submit_capped_order(
        market=market(),
        token_id="yes",
        cap_price=0.50,
        book=FakeBook(best_ask=0.49, depth=12),
        risk=state,
    )
    assert first.reason == "DRY_RUN"
    assert second.reason == "already_traded"


def test_guardrail_env_overrides_can_only_lower(monkeypatch) -> None:
    from polybot.config import load_settings

    monkeypatch.setenv("POLYBOT_MAX_ENTRY_PRICE", "0.99")
    monkeypatch.setenv("POLYBOT_PER_DAY_NOTIONAL", "100000")
    settings = load_settings()
    assert settings.guardrails.max_entry_price == 0.90
    assert settings.guardrails.per_day_notional == 100.0

    monkeypatch.setenv("POLYBOT_MAX_ENTRY_PRICE", "0.50")
    monkeypatch.setenv("POLYBOT_PER_DAY_NOTIONAL", "12")
    settings = load_settings()
    assert settings.guardrails.max_entry_price == 0.50
    assert settings.guardrails.per_day_notional == 12.0
