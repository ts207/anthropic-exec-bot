from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_binary_bot import _bot as _binary_bot, _config as _binary_config, _executor as _binary_executor, _signal as _binary_signal, article

from polybot.binary.decision import BinaryDecision
from polybot.core.book_snapshots import BookSnapshotLogger, NullBookSnapshotLogger, build_book_snapshot_logger
from polybot.core.config import SourcesConfig
from polybot.core.execution import DryRunTradingAdapter


def _fake_fetcher(books: dict[str, dict] | None = None, calls: list[str] | None = None):
    default = {
        "asks": [{"price": "0.42", "size": "100"}, {"price": "0.45", "size": "200"}],
        "bids": [{"price": "0.38", "size": "150"}, {"price": "0.35", "size": "50"}],
    }

    def fetch(token_id: str) -> dict:
        if calls is not None:
            calls.append(token_id)
        return (books or {}).get(token_id, default)

    return fetch


# ---- the logger itself ----


def test_snapshot_records_depth_and_context(tmp_path) -> None:
    logger = BookSnapshotLogger(tmp_path, fetcher=_fake_fetcher())
    logger.snapshot(["tok-1"], moment="gate_escalation", article_hash="h1", domain="reuters.com")
    record = json.loads((tmp_path / "book_snapshots.jsonl").read_text(encoding="utf-8"))
    assert record["moment"] == "gate_escalation"
    assert record["token_id"] == "tok-1"
    assert record["best_ask"] == pytest.approx(0.42)
    assert record["best_bid"] == pytest.approx(0.38)
    assert record["spread"] == pytest.approx(0.04)
    # depth in USD terms: 0.42*100 + 0.45*200 = 132; 0.38*150 + 0.35*50 = 74.5
    assert record["ask_depth_usd"] == pytest.approx(132.0)
    assert record["bid_depth_usd"] == pytest.approx(74.5)
    assert record["asks"][0] == [0.42, 100.0]
    assert record["article_hash"] == "h1" and record["domain"] == "reuters.com"


def test_snapshot_never_raises_into_the_trading_path(tmp_path) -> None:
    def broken(token_id: str) -> dict:
        raise RuntimeError("clob down")

    logger = BookSnapshotLogger(tmp_path, fetcher=broken)
    logger.snapshot(["tok-1"], moment="pre_order")  # must not raise
    assert not (tmp_path / "book_snapshots.jsonl").exists()


def test_builder_returns_null_logger_when_disabled(tmp_path) -> None:
    assert isinstance(build_book_snapshot_logger(tmp_path, False), NullBookSnapshotLogger)
    assert isinstance(build_book_snapshot_logger(tmp_path, True), BookSnapshotLogger)
    # The null logger writes nothing and fetches nothing.
    NullBookSnapshotLogger().snapshot(["tok-1"], moment="pre_order")
    assert not (tmp_path / "book_snapshots.jsonl").exists()


# ---- wiring: executor captures pre-order and post-fill books ----


def test_binary_entry_captures_pre_and_post_books(tmp_path) -> None:
    config = _binary_config(sources=SourcesConfig(log_book_snapshots=True))
    executor = _binary_executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    calls: list[str] = []
    executor.book_snapshots = BookSnapshotLogger(executor.store.data_dir, fetcher=_fake_fetcher(calls=calls))

    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _binary_signal())
    assert executor.execute(decision, article("The round will be held next week.")) == "ENTERED"

    lines = [json.loads(l) for l in (executor.store.data_dir / "book_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    moments = [l["moment"] for l in lines]
    assert moments == ["pre_order", "post_execution"]
    assert calls == ["yes-token", "yes-token"]
    assert lines[0]["action"] == "ENTER_YES"
    assert lines[0]["execution_id"] == lines[1]["execution_id"]  # joinable to the journal
    assert lines[1]["filled_shares"] > 0


def test_binary_exit_captures_books_around_the_sell(tmp_path) -> None:
    from polybot.binary.config import MarketConfig

    config = _binary_config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
        sources=SourcesConfig(log_book_snapshots=True),
    )
    executor = _binary_executor(tmp_path, config, DryRunTradingAdapter(yes_shares=250.0))
    executor.book_snapshots = BookSnapshotLogger(executor.store.data_dir, fetcher=_fake_fetcher())
    decision = BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed", _binary_signal(resolves_no=True))
    assert executor.execute(decision, article("Talks cancelled, will not happen.")) == "EXITED"
    lines = [json.loads(l) for l in (executor.store.data_dir / "book_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["moment"] for l in lines] == ["pre_order", "post_execution"]
    assert lines[1]["total_sold"] == pytest.approx(250.0)


def test_snapshots_disabled_by_default_makes_no_files(tmp_path) -> None:
    executor = _binary_executor(tmp_path, _binary_config(), DryRunTradingAdapter(yes_ask=0.40))
    assert isinstance(executor.book_snapshots, NullBookSnapshotLogger)
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _binary_signal())
    assert executor.execute(decision, article("The round will be held next week.")) == "ENTERED"
    assert not (executor.store.data_dir / "book_snapshots.jsonl").exists()


# ---- wiring: runner captures the gate-escalation moment ----


def test_gate_escalation_snapshot_precedes_classification(tmp_path) -> None:
    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0, log_book_snapshots=True),
    )
    bot = _binary_bot(tmp_path, config, DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    logger = BookSnapshotLogger(bot.store.data_dir, fetcher=_fake_fetcher())
    bot.book_snapshots = logger
    bot.executor.book_snapshots = logger

    entered = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert entered.action == "ENTER_YES"
    lines = [json.loads(l) for l in (bot.store.data_dir / "book_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    moments = [l["moment"] for l in lines]
    # The full repricing window in one file: stale book at escalation,
    # book at order time, book after the fill.
    assert moments == ["gate_escalation", "pre_order", "post_execution"]
    assert lines[0]["article_hash"]  # joinable to the article archive
    assert lines[0]["domain"] == "reuters.com"


def test_location_gate_escalation_snapshots_every_leg(tmp_path) -> None:
    from test_location_entry import _flat_config
    from polybot.location.runner import LocationProtectionBot

    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0, log_book_snapshots=True),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.38))
    calls: list[str] = []
    logger = BookSnapshotLogger(bot.store.data_dir, fetcher=_fake_fetcher(calls=calls))
    bot.book_snapshots = logger
    bot.executor.book_snapshots = logger

    bot.process_article(article("Officials confirm the next senior round will be held in Doha, Qatar next week."))
    lines = [json.loads(l) for l in (bot.store.data_dir / "book_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    gate_rows = [l for l in lines if l["moment"] == "gate_escalation"]
    # One snapshot per configured leg: the whole group state at the stale
    # moment (also feeds group-sum consistency analysis).
    assert {row["token_id"] for row in gate_rows} == {o.yes_token_id for o in config.outcomes}
