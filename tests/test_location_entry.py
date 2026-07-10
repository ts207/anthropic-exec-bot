from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from polybot.core.config import SourcesConfig
from polybot.core.execution import DryRunTradingAdapter
from polybot.core.operator import OperatorGate
from polybot.core.storage import StateStore
from polybot.core.types import Article
from polybot.location.config import (
    BuyRotationConfig,
    ClassifierConfig,
    EntryConfig,
    EventConfig,
    ExecutionConfig,
    LocationBotConfig,
    OutcomeMarket,
    PositionConfig,
    SellConfig,
    TimeDecayConfig,
    TriggerConfig,
    load_location_config,
)
from polybot.location.decision import LocationDecision, classify_agreement, entry_decision, final_decision
from polybot.location.executor import LocationExecutor
from polybot.location.runner import LocationProtectionBot


def article(text: str, domain: str = "reuters.com", title: str | None = None) -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title=title or text,
        published_at=None,
        fetched_at="2026-07-10T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((domain, text)))),
    )


def _outcomes() -> list[OutcomeMarket]:
    return [
        OutcomeMarket(name="qatar", label="Qatar", condition_id="0xqatar", yes_token_id="qatar-yes", no_token_id="qatar-no", rotation_target=True),
        OutcomeMarket(name="pakistan", label="Pakistan", condition_id="0xpk", yes_token_id="pk-yes", no_token_id="pk-no", rotation_target=True),
        OutcomeMarket(name="oman", label="Oman", condition_id="0xom", yes_token_id="om-yes", no_token_id="om-no", rotation_target=True),
        OutcomeMarket(name="no_meeting", label="No Meeting by September 30", condition_id="0xnm", yes_token_id="nm-yes", no_token_id="nm-no", rotation_target=False),
        OutcomeMarket(name="russia", label="Russia", condition_id="0xru", yes_token_id="ru-yes", no_token_id="ru-no", rotation_target=False),
    ]


def _flat_config(**overrides) -> LocationBotConfig:
    defaults = dict(
        event=EventConfig(
            slug="test-slug",
            question="Where will the next diplomatic US-Iran meeting be by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="",
            resolution_rules="test rules",
        ),
        outcomes=_outcomes(),
        position=PositionConfig(max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0),
        trigger=TriggerConfig(auto_execute_level=4, trusted_single_source_execution=True),
        classifier=ClassifierConfig(provider="rule_based"),
        entry=EntryConfig(enabled=True, targets=["qatar", "oman"], usd_budget=100.0, max_price=0.90, max_entries=1),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        time_decay=TimeDecayConfig(),
    )
    defaults.update(overrides)
    return LocationBotConfig(**defaults)


def _signal(**overrides):
    from polybot.location.types import LocationSignal

    defaults = dict(
        source_is_trusted=True,
        qualifies_as_senior_round=True,
        round_status="scheduled",
        location_country_name="Qatar",
        confirmed_location="qatar",
        evidence_strength="confirmed_scheduled",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=False,
        level="4A",
        quote_supporting_trigger="A new round will begin in Doha.",
        source_tier="wire",
    )
    defaults.update(overrides)
    return LocationSignal(**defaults)


def _executor(tmp_path, config: LocationBotConfig, adapter: DryRunTradingAdapter) -> LocationExecutor:
    store = StateStore(tmp_path / "state")
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    executor = LocationExecutor(config, store, _Notifier(), adapter)
    executor._notified = notified  # type: ignore[attr-defined]
    return executor


# ---- entry decision table ----


def test_confirmed_entry_target_tier_one_strong_triggers_enter() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "qatar"
    assert decision.reason == "confirmed_location:qatar"


def test_weak_evidence_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="speculative"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:qatar"


def test_non_tier_one_source_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_scheduled", source_tier="other"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:qatar"


def test_configured_non_entry_target_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_target_not_allowed:pakistan"


def test_unconfigured_location_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="other_specific", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "confirmed_location_not_configured:other_specific"


def test_entry_disabled_is_alert_only() -> None:
    config = _flat_config(entry=EntryConfig(enabled=False))
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_disabled_confirmed_location:qatar"


def test_untrusted_source_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(source_is_trusted=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_technical_only_is_no_action() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(round_status="technical_only", qualifies_as_senior_round=False, confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"


def test_no_location_signal_is_no_action() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="none"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "no_location_signal"


def test_no_meeting_not_an_entry_target_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(
        config,
        _signal(confirmed_location="no_meeting", qualifies_as_senior_round=False, evidence_strength="denied", source_tier="official_government"),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "no_meeting_reported_while_flat"


def test_no_meeting_as_entry_target_tier_one_triggers_enter() -> None:
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["no_meeting"]))
    decision = entry_decision(
        config,
        _signal(confirmed_location="no_meeting", qualifies_as_senior_round=False, evidence_strength="denied", source_tier="official_government"),
    )
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "no_meeting"


def test_venue_not_final_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(
        config,
        _signal(confirmed_location="qatar", evidence_strength="confirmed_scheduled", source_tier="wire", final_decision_announced=False),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_venue_not_final:qatar"


# ---- classify_agreement while flat ----


def test_flat_agreement_pass_disagreement_is_alert_only() -> None:
    config = _flat_config()
    passes = [
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
        _signal(confirmed_location="oman", evidence_strength="confirmed_started", source_tier="wire"),
    ]
    decision = classify_agreement(config, passes, held=None, flat=True)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


def test_flat_agreement_matching_passes_enter() -> None:
    config = _flat_config()
    passes = [
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
    ]
    decision = classify_agreement(config, passes, held=None, flat=True)
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "qatar"


def test_flat_collapse_fast_path_disabled_requires_agreement() -> None:
    # While holding, a tier-one collapse fires on the first pass alone; while
    # flat there is no loss to protect, so an entry into the no-meeting leg
    # must still meet the full agreement bar.
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["no_meeting"]))
    collapse = _signal(
        confirmed_location="no_meeting",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="official_government",
        quote_supporting_trigger="Talks are cancelled entirely.",
    )
    disagreeing = _signal(confirmed_location="none", evidence_strength="speculative", source_tier="official_government", qualifies_as_senior_round=False)
    decision = classify_agreement(config, [collapse, disagreeing], held=None, flat=True)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


def test_held_agreement_still_routes_to_protection() -> None:
    config = _flat_config()
    passes = [_signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire")] * 2
    decision = classify_agreement(config, passes, held="qatar", flat=False)
    assert decision.action == "ROTATE_YES"
    assert decision.target_outcome == "pakistan"


# ---- executor entry path ----


def test_executor_enters_and_records_holding(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    assert executor.holdings.held_location() is None
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTERED"
    current = executor.store.current()
    assert current is not None and current.state == "ENTERED"
    assert current.payload["target_outcome"] == "qatar"
    assert executor.holdings.held_location() == "qatar"
    assert executor.holdings.record().source == "entry"
    assert executor.entry_count() == 1


def test_executor_entry_price_above_cap_stays_flat(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.95)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTRY_PRICE_ABOVE_CAP"
    assert executor.holdings.held_location() is None
    assert executor.entry_count() == 0


def test_executor_global_guardrail_clamps_entry_cap(tmp_path) -> None:
    # SETTINGS.guardrails.max_entry_price defaults to 0.90 and can only be
    # lowered; a config cap of 0.99 must not buy through it.
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["qatar"], max_price=0.99))
    adapter = DryRunTradingAdapter(yes_ask=0.95)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTRY_PRICE_ABOVE_CAP"


def test_executor_entry_skipped_when_already_holding(tmp_path) -> None:
    config = _flat_config(
        event=EventConfig(
            slug="test-slug",
            question="q",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
    )
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:oman", target_outcome="oman", factors=_signal(confirmed_location="oman"))
    result = executor.execute(decision, article("Officials confirm the round will be held in Oman."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() == "qatar"


def test_executor_entry_skipped_for_non_entry_target(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round will be held in Pakistan."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() is None


def test_executor_entry_respects_max_entries(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"
    # Simulate a manual flat reset without resetting the entry budget.
    executor.holdings.clear_held(source="exit")
    result = executor.execute(decision, article("Officials re-confirm the round will be held in Qatar."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() is None


def test_entry_then_defend_rotates_the_entered_leg(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    enter = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(enter, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"

    # The venue shifts: with the live holding now qatar, the same protection
    # machinery must sell the ENTERED leg and rotate.
    signal = _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire")
    decision = final_decision(config, signal, held=executor.holdings.held_location())
    assert decision.action == "ROTATE_YES"
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    assert current.payload["from_outcome"] == "qatar"
    assert current.payload["to_outcome"] == "pakistan"
    assert executor.holdings.held_location() == "pakistan"
    assert executor.holdings.record().source == "rotation"


def test_exit_clears_holding_and_one_shot_blocks_reentry(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    enter = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(enter, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"

    exit_decision = LocationDecision(
        "EXIT_YES_ONLY", "4B", "confirmed_non_held_location_not_rotated:russia", factors=_signal(confirmed_location="russia")
    )
    assert executor.execute(exit_decision, article("Officials confirm the round begins in Russia.")) == "EXITED"
    assert executor.holdings.held_location() is None
    assert executor.holdings.record().source == "exit"

    # one_shot: the terminal EXITED state blocks a fresh entry.
    result = executor.execute(enter, article("Officials now confirm the round will be held in Qatar after all."))
    assert result == "EXITED"
    assert executor.holdings.held_location() is None


# ---- operator gate ----


def test_operator_gate_blocks_enter_yes_by_default(tmp_path) -> None:
    config = _flat_config(data_dir=tmp_path / "data")
    config_path = tmp_path / "entry.yaml"
    config_path.write_text("entry-config\n", encoding="utf-8")
    gate = OperatorGate(config_path, config)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar")
    result = gate.check(decision, live_requested=True)
    assert not result.allowed


# ---- runner flow ----


def test_bot_enters_then_reinforces_held_location(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40))
    first = bot.process_article(article("US and Iran senior negotiators scheduled talks: the round will be held in Qatar."))
    assert first.action == "ENTER_YES"
    assert bot.holdings.held_location() == "qatar"

    # Same news again: now the bot is holding, so the identical confirmation
    # is routed through the protection table and reinforces the held leg.
    second = bot.process_article(article("Senior negotiators re-confirm the talks round will be held in Qatar.", title="second"))
    assert second.action == "NO_ACTION"
    assert second.reason == "held_location_reinforced"


def test_bot_max_entries_reached_is_alert_only(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40))
    first = bot.process_article(article("US and Iran senior negotiators scheduled talks: the round will be held in Qatar."))
    assert first.action == "ENTER_YES"
    # Force flat again without resetting the entry budget.
    bot.holdings.clear_held(source="exit")
    second = bot.process_article(article("Fresh reports say the talks round will be held in Oman.", title="oman"))
    assert second.action == "ALERT_ONLY"
    assert second.reason == "max_entries_reached:1"


# ---- config loading/validation ----


def _yaml_config(tmp_path: Path, *, held_location: str, entry_block: str) -> Path:
    path = tmp_path / "config.yaml"
    base = textwrap.dedent(
        f"""
        event:
          slug: "test-slug"
          question: "Where will the meeting be?"
          deadline_date: "2026-09-30"
          held_location: "{held_location}"
        outcomes:
          - name: qatar
            label: "Qatar"
            condition_id: "0xqatar"
            yes_token_id: "qatar-yes"
            no_token_id: "qatar-no"
            rotation_target: true
          - name: oman
            label: "Oman"
            condition_id: "0xom"
            yes_token_id: "om-yes"
            no_token_id: "om-no"
        """
    )
    path.write_text(base + entry_block, encoding="utf-8")
    return path


def test_load_config_parses_entry_section(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["Qatar", "oman"]
              usd_budget: 75.0
              max_price: 0.85
              max_entries: 2
            """
        ),
    )
    config = load_location_config(path)
    assert config.entry.enabled is True
    assert config.entry_target_names() == {"qatar", "oman"}
    assert config.entry.usd_budget == 75.0
    assert config.entry.max_price == 0.85
    assert config.entry.max_entries == 2


def test_load_config_rejects_unknown_entry_target(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["atlantis"]
            """
        ),
    )
    with pytest.raises(ValueError, match="entry.targets not found in outcomes"):
        load_location_config(path)


def test_load_config_rejects_flat_without_entry(tmp_path) -> None:
    path = _yaml_config(tmp_path, held_location="", entry_block="")
    with pytest.raises(ValueError, match="nothing to protect or enter"):
        load_location_config(path)


def test_load_config_rejects_entry_without_targets(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
            """
        ),
    )
    with pytest.raises(ValueError, match="at least one entry.targets"):
        load_location_config(path)


def test_load_config_rejects_held_location_in_entry_targets(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="qatar",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["qatar"]
            """
        ),
    )
    with pytest.raises(ValueError, match="must not be listed in entry.targets"):
        load_location_config(path)
