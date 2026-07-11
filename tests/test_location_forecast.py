from __future__ import annotations

from pathlib import Path

import pytest

from polybot.core.execution import DryRunTradingAdapter
from polybot.core.types import Article
from polybot.location.config import (
    EntryConfig,
    EventConfig,
    ForecastConfig,
    LocationBotConfig,
    OutcomeMarket,
    _validate_forecast,
)
from polybot.location.forecast import ForecastPaperEngine
from polybot.location.calibration import evaluate_probability_state
from polybot.location.types import LocationSignal


def _config(tmp_path: Path, **forecast_overrides) -> LocationBotConfig:
    forecast = ForecastConfig(
        enabled=True,
        paper_only=True,
        prior_probabilities={"qatar": 0.30, "oman": 0.70},
        min_paper_edge=0.12,
        max_paper_price=0.70,
        paper_order_usd=10.0,
        **forecast_overrides,
    )
    return LocationBotConfig(
        event=EventConfig(slug="event", question="where", deadline_date="2026-09-30", held_location=""),
        outcomes=[
            OutcomeMarket("qatar", "Qatar", "cq", "yq", "nq", True),
            OutcomeMarket("oman", "Oman", "co", "yo", "no", True),
        ],
        entry=EntryConfig(enabled=True, targets=["qatar", "oman"]),
        forecast=forecast,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )


def _article(name: str) -> Article:
    return Article(
        url=f"https://reuters.com/{name}",
        domain="reuters.com",
        title=name,
        published_at="2026-07-10T00:00:00Z",
        fetched_at="2026-07-10T00:00:01Z",
        raw_text=(
            name
            + "\nOfficials expect the formal round in Doha."
            + "\nOfficials now expect the formal round in Muscat."
            + "\nOfficials deny the formal round will be held in Doha."
        ),
        hash=name,
    )


def _signal(**overrides) -> LocationSignal:
    values = dict(
        source_is_trusted=True,
        qualifies_as_senior_round=False,
        round_status="rumor",
        location_country_name="Qatar",
        confirmed_location="none",
        evidence_strength="reported_indirect",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=False,
        level="2",
        quote_supporting_trigger="Officials expect the formal round in Doha.",
        source_tier="wire",
        headline_location="qatar",
        technical_location="none",
        future_expected_formal_location="qatar",
        final_decision_announced=False,
        forecast_target_location="qatar",
        evidence_direction="supportive",
    )
    values.update(overrides)
    return LocationSignal(**values)


def test_anticipatory_evidence_updates_probability_and_opens_paper_entry(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.20, yes_bid=0.10), config.data_dir, config.logs_dir)
    signal = _signal()
    result = engine.process(_article("first"), [signal, signal])
    assert result["updated"] is True
    assert result["paper_only"] is True
    assert result["observation"]["after"]["qatar"] > 0.30
    assert result["opened"]["side"] == "BUY_YES"
    assert result["opened"]["outcome"] == "qatar"
    assert engine.snapshot()["positions"]["qatar"]["cost_usd"] == 10.0


def test_final_confirmation_updates_probability_but_does_not_use_forecast_entry(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.20), config.data_dir, config.logs_dir)
    signal = _signal(
        qualifies_as_senior_round=True,
        round_status="scheduled",
        confirmed_location="qatar",
        evidence_strength="confirmed_scheduled",
        final_decision_announced=True,
    )
    result = engine.process(_article("confirmed"), [signal, signal])
    assert result["updated"] is True
    assert result["opened"] is None


def test_classifier_disagreement_does_not_update_forecast(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(), config.data_dir, config.logs_dir)
    result = engine.process(
        _article("disagree"),
        [_signal(), _signal(future_expected_formal_location="oman")],
    )
    assert result == {"enabled": True, "updated": False, "reason": "classifier_pass_disagreement"}


def test_article_and_claim_deduplication_prevent_evidence_double_count(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.20), config.data_dir, config.logs_dir)
    signal = _signal()
    first = engine.process(_article("one"), [signal, signal])
    after = first["observation"]["after"]["qatar"]
    repeated_article = engine.process(_article("one"), [signal, signal])
    assert repeated_article["reason"] == "article_already_processed"
    repeated_claim = engine.process(_article("syndicated"), [signal, signal])
    assert repeated_claim["reason"] == "claim_already_processed"
    assert engine.snapshot()["probabilities"]["qatar"] == pytest.approx(after)


def test_probability_reversal_exits_open_paper_position(tmp_path) -> None:
    config = _config(tmp_path)
    adapter = DryRunTradingAdapter(yes_ask=0.20, yes_bid=0.10)
    engine = ForecastPaperEngine(config, adapter, config.data_dir, config.logs_dir)
    first = _signal()
    assert engine.process(_article("open"), [first, first])["opened"] is not None
    adapter.yes_bid_value = 0.50
    reverse = _signal(
        location_country_name="Oman",
        future_expected_formal_location="oman",
        quote_supporting_trigger="Officials now expect the formal round in Muscat.",
        evidence_strength="confirmed_scheduled",
        source_tier="official_government",
        forecast_target_location="oman",
    )
    result = engine.process(_article("reverse"), [reverse, reverse])
    qatar_exits = [trade for trade in result["exits"] if trade["outcome"] == "qatar"]
    assert len(qatar_exits) == 1
    assert "qatar" not in engine.snapshot()["positions"]


def test_contradictory_denial_reduces_target_probability(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.20, yes_bid=0.10), config.data_dir, config.logs_dir)
    denial = _signal(
        evidence_strength="denied",
        evidence_direction="contradictory",
        forecast_target_location="qatar",
        quote_supporting_trigger="Officials deny the formal round will be held in Doha.",
        source_tier="official_government",
    )
    result = engine.process(_article("denial"), [denial, denial])
    assert result["observation"]["likelihood_ratio"] < 1.0
    assert result["observation"]["after"]["qatar"] < 0.30
    assert result["opened"] is None


def test_forecast_requires_verbatim_supporting_quote(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(), config.data_dir, config.logs_dir)
    signal = _signal(quote_supporting_trigger="This sentence is not in the article.")
    result = engine.process(_article("quote-missing"), [signal, signal])
    assert result["reason"] == "quote_verification_failed"
    assert engine.snapshot()["observation_count"] == 0


def test_market_mark_can_exit_without_new_article(tmp_path) -> None:
    config = _config(tmp_path)
    adapter = DryRunTradingAdapter(yes_ask=0.20, yes_bid=0.10)
    engine = ForecastPaperEngine(config, adapter, config.data_dir, config.logs_dir)
    signal = _signal()
    assert engine.process(_article("mark-open"), [signal, signal])["opened"] is not None
    adapter.yes_bid_value = 0.90
    result = engine.mark_cycle()
    assert result["updated"] is True
    assert result["exits"][0]["trigger_kind"] == "market_mark"
    assert engine.snapshot()["positions"] == {}


def test_wide_quote_spread_blocks_paper_entry(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.60, yes_bid=0.10), config.data_dir, config.logs_dir)
    signal = _signal()
    result = engine.process(_article("wide-spread"), [signal, signal])
    assert result["updated"] is True
    assert result["opened"] is None


def test_model_version_change_archives_incompatible_probability_state(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(), config.data_dir, config.logs_dir)
    signal = _signal()
    engine.process(_article("version-one"), [signal, signal])
    changed = _config(tmp_path, model_version="location-forecast-v3")
    replacement = ForecastPaperEngine(changed, DryRunTradingAdapter(), changed.data_dir, changed.logs_dir)
    assert replacement.snapshot()["observation_count"] == 0
    assert list(changed.data_dir.glob("forecast_probability.incompatible-*.json"))


def test_resolved_outcome_evaluator_reports_proper_scoring_rules(tmp_path) -> None:
    config = _config(tmp_path)
    engine = ForecastPaperEngine(config, DryRunTradingAdapter(yes_ask=0.20, yes_bid=0.10), config.data_dir, config.logs_dir)
    signal = _signal()
    engine.process(_article("evaluate"), [signal, signal])
    report = evaluate_probability_state(config.data_dir / "forecast_probability.json", "qatar")
    assert report["observation_count"] == 1
    assert report["mean_brier_score"] >= 0
    assert report["mean_log_loss"] >= 0
    assert "correlated" in report["caveat"]


def test_forecast_is_technically_forced_to_paper_only(tmp_path) -> None:
    config = _config(tmp_path)
    object.__setattr__(config, "forecast", ForecastConfig(
        enabled=True,
        paper_only=False,
        prior_probabilities={"qatar": 0.3, "oman": 0.7},
    ))
    with pytest.raises(ValueError, match="paper_only must remain true"):
        _validate_forecast(config)


def test_forecast_priors_must_cover_complete_categorical_market(tmp_path) -> None:
    config = _config(tmp_path)
    object.__setattr__(config, "forecast", ForecastConfig(
        enabled=True,
        prior_probabilities={"qatar": 1.0},
    ))
    with pytest.raises(ValueError, match="cover every outcome exactly"):
        _validate_forecast(config)
