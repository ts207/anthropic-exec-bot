from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from polybot.gamma import MarketMeta
from polybot.iran.executor import DryRunTradingAdapter, LivePosition
from polybot.portfolio import load_portfolio_config, snapshot_portfolio


class SnapshotAdapter(DryRunTradingAdapter):
    def __init__(self) -> None:
        super().__init__(yes_shares=12.5, no_shares=3.0, yes_ask=0.47, no_ask=0.54, yes_bid=0.46)

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, yes_shares=12.5, no_shares=3.0)

    def open_orders_for_market(self, condition_id: str) -> list[dict[str, Any]]:
        return [{"id": "order-1", "market": condition_id, "side": "SELL", "asset_id": "yes"}]


def test_load_portfolio_config_accepts_market_slug_alias(tmp_path: Path) -> None:
    path = tmp_path / "positions.yaml"
    path.write_text(
        """
positions:
  - id: p1
    market_slug: event-slug
    held_side: YES
""",
        encoding="utf-8",
    )

    config = load_portfolio_config(path)

    assert config.positions[0].event_slug == "event-slug"
    assert config.positions[0].mode == "alert_only"


def test_snapshot_portfolio_includes_position_risk_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "positions.yaml"
    path.write_text(
        """
positions:
  - id: iran-july17-yes
    event_slug: iran-event
    expected_question_contains: July 17
    held_side: YES
    strategy: news_deadline_protection
    expected_yes_token_id: yes
    expected_no_token_id: no
    max_yes_shares_to_sell: 12.5
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("polybot.portfolio.select_market", lambda slug, question_contains=None: _market())

    snapshot = snapshot_portfolio(path, adapter=SnapshotAdapter())

    position = snapshot["positions"][0]
    assert snapshot["summary"]["count"] == 1
    assert snapshot["summary"]["live_yes_shares"] == 12.5
    assert position["id"] == "iran-july17-yes"
    assert position["status"] == "blocked"
    assert position["live_position"]["yes_shares"] == 12.5
    assert position["open_orders"][0]["id"] == "order-1"
    assert position["book"]["yes"]["best_bid"] == 0.46
    assert position["book"]["yes"]["best_ask"] == 0.47
    assert "operator_mode_alert_only" in position["operator"]["blockers"]


def test_snapshot_portfolio_reports_market_errors_per_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "positions.yaml"
    path.write_text(
        """
positions:
  - id: p1
    event_slug: event
    held_side: NO
""",
        encoding="utf-8",
    )

    def fail(_slug: str, question_contains: str | None = None) -> MarketMeta:
        raise ValueError("market missing")

    monkeypatch.setattr("polybot.portfolio.select_market", fail)

    snapshot = snapshot_portfolio(path, adapter=SnapshotAdapter())

    assert snapshot["summary"]["errors"] == 1
    assert snapshot["positions"][0]["status"] == "needs_attention"
    assert snapshot["positions"][0]["errors"][0]["phase"] == "market"


def test_snapshot_portfolio_can_filter_one_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "positions.yaml"
    path.write_text(
        """
positions:
  - id: p1
    event_slug: event
    held_side: YES
  - id: p2
    event_slug: event
    held_side: NO
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("polybot.portfolio.select_market", lambda slug, question_contains=None: _market())

    snapshot = snapshot_portfolio(path, adapter=SnapshotAdapter(), position_id="p2")

    assert [position["id"] for position in snapshot["positions"]] == ["p2"]


def test_snapshot_portfolio_missing_position_id_raises(tmp_path: Path) -> None:
    path = tmp_path / "positions.yaml"
    path.write_text("positions: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="position id not found"):
        snapshot_portfolio(path, adapter=SnapshotAdapter(), position_id="missing")


def _market() -> MarketMeta:
    return MarketMeta(
        event_slug="iran-event",
        market_slug="july-17",
        condition_id="cond",
        question="US x Iran diplomatic meeting by July 17, 2026?",
        description="rules",
        resolution_source="source",
        outcomes=["Yes", "No"],
        outcome_prices=[0.47, 0.53],
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
