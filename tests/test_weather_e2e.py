from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from polybot.gamma import MarketMeta
from polybot.risk import RiskState
from polybot.sources.base import SourceReading
from polybot.strategies.weather import WeatherStrategy


class FakeAdapter:
    def __init__(self, value: float = 24, raw_value: float | None = None):
        self.value = value
        self.raw_value = raw_value

    def poll(self) -> SourceReading:
        return SourceReading(
            value=self.value,
            unit="C",
            is_locked=True,
            confidence=1.0,
            raw_url="https://example.test",
            fetched_at=datetime.now(timezone.utc),
            raw_value=self.raw_value,
        )


class FakeBook:
    def best_ask(self, _token_id: str) -> float:
        return 0.5

    def staleness(self, _token_id: str) -> float:
        return 0.0

    def depth_under_cap(self, _token_id: str, _cap: float) -> float:
        return 10.0

    def rest_snapshot(self, _token_id: str) -> None:
        return None

    def snapshot_state(self, _token_id: str) -> dict:
        return {"best_ask": 0.5}


def test_weather_strategy_locked_reading_submits_one_dry_run(tmp_path: Path, monkeypatch) -> None:
    events = []

    def capture(event: str, **fields):
        events.append((event, fields))

    monkeypatch.setattr("polybot.exec_engine.log_event", capture)
    monkeypatch.setattr("polybot.strategies.weather.log_event", capture)
    market = MarketMeta(
        event_slug="",
        market_slug="m",
        condition_id="c",
        question="Will the highest temperature be 24°C on July 3?",
        description="contains the highest temperature; whole degrees; Revisions considered until lock",
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
        revisable_rule=True,
    )
    strategy = WeatherStrategy(
        markets=[market],
        adapter=FakeAdapter(),
        book_factory=lambda _market: FakeBook(),
        risk=RiskState(path=tmp_path / "risk.json"),
    )
    strategy.run_once()
    order_events = [fields for event, fields in events if event == "order_submit"]
    assert len(order_events) == 1
    assert order_events[0]["dry_run"] is True
    assert order_events[0]["args"]["token_id"] == "yes"
    assert order_events[0]["args"]["price"] == 0.85

    strategy.run_once()
    order_events = [fields for event, fields in events if event == "order_submit"]
    assert len(order_events) == 1


def test_weather_strategy_skips_raw_values_too_close_to_bucket_edge(tmp_path: Path, monkeypatch) -> None:
    events = []

    def capture(event: str, **fields):
        events.append((event, fields))

    monkeypatch.setattr("polybot.exec_engine.log_event", capture)
    monkeypatch.setattr("polybot.strategies.weather.log_event", capture)
    market = MarketMeta(
        event_slug="",
        market_slug="m",
        condition_id="c",
        question="Will the highest temperature be 24°C on July 3?",
        description="whole degrees",
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
        revisable_rule=True,
    )
    strategy = WeatherStrategy(
        markets=[market],
        adapter=FakeAdapter(value=24, raw_value=24.49),
        book_factory=lambda _market: FakeBook(),
        risk=RiskState(path=tmp_path / "risk.json"),
    )
    strategy.run_once()
    assert [event for event, _fields in events if event == "order_submit"] == []
    skip_events = [fields for event, fields in events if event == "strategy_skip"]
    assert skip_events[-1]["reason"] == "boundary_margin"
