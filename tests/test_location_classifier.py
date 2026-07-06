from __future__ import annotations

import json
from datetime import date

import pytest

from polybot.iran.executor import DryRunTradingAdapter
from polybot.iran.storage import StateStore
from polybot.iran.types import Article
from polybot.location.classifier import LLMLocationClassifier, RuleBasedFixtureLocationClassifier
from polybot.location.config import (
    ClassifierConfig,
    EventConfig,
    ExecutionConfig,
    LocationBotConfig,
    OutcomeMarket,
    PositionConfig,
    SellConfig,
    BuyRotationConfig,
    TimeDecayConfig,
    TriggerConfig,
)
from polybot.location.decision import LocationDecision, classify_agreement, final_decision, time_decay_decision
from polybot.location.executor import LocationExecutor
from polybot.location.types import LocationSignal
from polybot.location import market_verifier as market_verifier_mod
from polybot.location.market_verifier import verify_critical_outcomes, verify_location_event


def article(text: str, domain: str = "reuters.com", title: str | None = None) -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title=title or text,
        published_at=None,
        fetched_at="2026-07-06T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((domain, text)))),
    )


def _outcomes() -> list[OutcomeMarket]:
    return [
        OutcomeMarket(name="qatar", label="Qatar", condition_id="0xqatar", yes_token_id="qatar-yes", no_token_id="qatar-no", rotation_target=True),
        OutcomeMarket(name="pakistan", label="Pakistan", condition_id="0xpk", yes_token_id="pk-yes", no_token_id="pk-no", rotation_target=True),
        OutcomeMarket(name="switzerland", label="Switzerland", condition_id="0xch", yes_token_id="ch-yes", no_token_id="ch-no", rotation_target=True),
        OutcomeMarket(name="oman", label="Oman", condition_id="0xom", yes_token_id="om-yes", no_token_id="om-no", rotation_target=True),
        OutcomeMarket(name="no_meeting", label="No Meeting by September 30", condition_id="0xnm", yes_token_id="nm-yes", no_token_id="nm-no", rotation_target=False),
        OutcomeMarket(name="russia", label="Russia", condition_id="0xru", yes_token_id="ru-yes", no_token_id="ru-no", rotation_target=False),
    ]


def _config(**overrides) -> LocationBotConfig:
    defaults = dict(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
        outcomes=_outcomes(),
        position=PositionConfig(held_yes_shares=1000.0, max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0),
        trigger=TriggerConfig(auto_execute_level=4, trusted_single_source_execution=True),
        classifier=ClassifierConfig(provider="rule_based"),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        time_decay=TimeDecayConfig(),
    )
    defaults.update(overrides)
    return LocationBotConfig(**defaults)


def _signal(**overrides) -> LocationSignal:
    defaults = dict(
        source_is_trusted=True,
        qualifies_as_senior_round=True,
        round_status="scheduled",
        location_country_name="Qatar",
        confirmed_location="qatar",
        evidence_strength="confirmed_scheduled",
        would_resolve_held_location_yes=True,
        would_resolve_held_location_no=False,
        level="4A",
        quote_supporting_trigger="A new round will begin in Doha.",
        source_tier="wire",
    )
    defaults.update(overrides)
    return LocationSignal(**defaults)


# ---- decision engine ----


def test_held_location_confirmed_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "held_location_reinforced"


def test_rotation_target_confirmed_strong_triggers_rotate() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ROTATE_YES"
    assert decision.target_outcome == "pakistan"


def test_rotation_target_confirmed_weak_evidence_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="speculative"))
    assert decision.action == "ALERT_ONLY"
    assert "not_yet_confirmed" in decision.reason


def test_rotation_target_confirmed_non_tier_one_source_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_scheduled", source_tier="other"))
    assert decision.action == "ALERT_ONLY"


def test_untracked_location_confirmed_is_sell_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="russia", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.target_outcome is None


def test_no_meeting_confirmed_is_sell_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="no_meeting", evidence_strength="confirmed_started", source_tier="official_government"))
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_technical_only_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(round_status="technical_only", qualifies_as_senior_round=False, confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "technical_or_non_qualifying"


def test_untrusted_source_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(source_is_trusted=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_no_location_signal_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="none"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "no_location_signal"


def test_time_decay_disabled_by_default() -> None:
    config = _config()
    assert time_decay_decision(config).action == "NO_ACTION"


def test_time_decay_trim_and_exit() -> None:
    config = _config(time_decay=TimeDecayConfig(enabled=True, trim_after_date="2026-09-16", exit_after_date="2026-09-23"))
    assert time_decay_decision(config, today=date(2026, 9, 1)).action == "NO_ACTION"
    assert time_decay_decision(config, today=date(2026, 9, 17)).action == "TRIM_YES"
    assert time_decay_decision(config, today=date(2026, 9, 24)).action == "EXIT_YES_ONLY"


# ---- rule-based classifier ----


def test_rule_based_classifier_detects_held_location() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    signal = classifier.classify(article("The next round of talks will begin in Qatar next week."), "rules")
    assert signal.confirmed_location == "qatar"
    assert signal.would_resolve_held_location_yes is True


def test_rule_based_classifier_detects_rotation_target() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    signal = classifier.classify(article("Officials confirm the next round will begin in Pakistan."), "rules")
    assert signal.confirmed_location == "pakistan"
    assert signal.would_resolve_held_location_no is True


# ---- executor ----


def _executor(tmp_path, config: LocationBotConfig, adapter: DryRunTradingAdapter) -> LocationExecutor:
    store = StateStore(tmp_path / "state")
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    executor = LocationExecutor(config, store, _Notifier(), adapter)
    executor._notified = notified  # type: ignore[attr-defined]
    return executor


def test_executor_rotate_sells_held_and_buys_target(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    assert current.state == "ROTATED"
    assert current.payload["from_outcome"] == "qatar"
    assert current.payload["to_outcome"] == "pakistan"


def test_executor_rotate_skips_buy_when_target_price_above_cap(tmp_path) -> None:
    config = _config(execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig(max_price=0.10)))
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.90)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "EXITED"


def test_executor_exit_only_sells_without_buying(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    result = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    assert result == "EXITED"


def test_executor_one_shot_blocks_second_trade(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    first = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    second = executor.execute(decision, article("No qualifying round will occur before the deadline, again."))
    assert first == "EXITED"
    assert second == "EXITED"  # terminal state returned, no duplicate sell
    assert adapter.yes_shares == 1000.0  # DryRunTradingAdapter doesn't mutate balances; sanity that no crash occurred on 2nd call


def test_executor_no_position_skips_trade(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=0.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    result = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    assert result == "YES_POSITION_UNCONFIRMED"


# ---- claude_cli provider (mirrors iran classifier's coverage) ----


def _claude_cli_classifier(cli_runner) -> LLMLocationClassifier:
    config = _config(classifier=ClassifierConfig(provider="claude_cli", model="claude-sonnet-4-6"))
    return LLMLocationClassifier(config.classifier, config, cli_runner=cli_runner)


def test_claude_cli_location_classifier_parses_structured_output() -> None:
    payload = {
        "source_is_trusted": True,
        "qualifies_as_senior_round": True,
        "round_status": "scheduled",
        "location_country_name": "Pakistan",
        "confirmed_location": "pakistan",
        "evidence_strength": "confirmed_scheduled",
        "would_resolve_held_location_yes": False,
        "would_resolve_held_location_no": True,
        "level": "4A",
        "quote_supporting_trigger": "The next round will begin in Islamabad.",
        "source_tier": "wire",
    }
    envelope = json.dumps({"type": "result", "is_error": False, "result": "see structured_output", "structured_output": payload})
    classifier = _claude_cli_classifier(lambda prompt: envelope)
    signal = classifier.classify(article("The next round will begin in Islamabad."), "rules")
    assert signal.confirmed_location == "pakistan"
    assert signal.level == "4A"


def test_claude_cli_location_classifier_error_envelope_raises() -> None:
    classifier = _claude_cli_classifier(lambda prompt: json.dumps({"type": "result", "is_error": True, "result": "boom"}))
    with pytest.raises(RuntimeError, match="claude CLI reported an error"):
        classifier.classify(article("test"), "rules")


def test_prompt_includes_analyst_context_when_set() -> None:
    from polybot.location.classifier import _prompt

    config = _config(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
            analyst_context="Pakistan meeting is technical/non-resolving; watch for the delayed Doha round instead.",
        )
    )
    prompt = _prompt(article("test"), "rules", config)
    assert "Analyst context" in prompt
    assert "delayed Doha round" in prompt


def test_prompt_omits_analyst_context_section_when_blank() -> None:
    from polybot.location.classifier import _prompt

    config = _config()  # default EventConfig has analyst_context=""
    prompt = _prompt(article("test"), "rules", config)
    assert "Analyst context" not in prompt


# ---- rotation buy capped by confirmed sale proceeds (2026-07-06 hardening) ----


def test_rotation_buy_capped_by_confirmed_proceeds_not_full_budget(tmp_path) -> None:
    # Configured budget/cap allow up to $500, but the held outcome only has a
    # $0.10 best bid -- confirmed proceeds (1000 * 0.10 = $100) must cap the
    # rotation buy, not the full $500 configured budget.
    config = _config(position=PositionConfig(held_yes_shares=1000.0, max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0))
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40, yes_bid=0.10)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    # confirmed proceeds (1000 shares * $0.10 bid = $100) cap the buy, so the
    # filled shares reflect a $100 order at the default 0.95 cap, not a $500 one.
    assert current.payload["rotation_filled_shares"] == pytest.approx(100.0 / 0.95)


def test_rotation_buy_skipped_when_proceeds_too_small(tmp_path) -> None:
    # Best bid near zero -> confirmed proceeds below the minimum viable order
    # size -> sell-only, no rotation buy attempted at all.
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40, yes_bid=0.0005)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "EXITED"
    current = executor.store.current()
    assert current.payload["reason"] == "insufficient_sale_proceeds_for_rotation_buy"


# ---- 2-pass classifier agreement (2026-07-06 hardening) ----


def test_classify_agreement_fires_when_passes_agree() -> None:
    config = _config()
    signal = _signal(confirmed_location="pakistan", round_status="scheduled")
    decision = classify_agreement(config, [signal, signal])
    assert decision.action == "ROTATE_YES"


def test_classify_agreement_alerts_on_disagreement() -> None:
    config = _config()
    first = _signal(confirmed_location="pakistan", round_status="scheduled")
    second = _signal(confirmed_location="qatar", round_status="scheduled")
    decision = classify_agreement(config, [first, second])
    assert decision.action == "ALERT_ONLY"
    assert "classifier_pass_disagreement" in decision.reason
    assert "confirmed_location" in decision.reason


def test_classify_agreement_no_meeting_fast_paths_on_first_pass() -> None:
    config = _config()
    collapse = _signal(
        confirmed_location="no_meeting",
        round_status="none",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="wire",
        quote_supporting_trigger="Iran officially terminated the negotiation process.",
    )
    other = _signal(confirmed_location="pakistan")  # would disagree if agreement were required
    decision = classify_agreement(config, [collapse, other])
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_classify_agreement_no_meeting_with_delay_language_not_fast_pathed() -> None:
    config = _config()
    ambiguous = _signal(
        confirmed_location="no_meeting",
        round_status="none",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="wire",
        quote_supporting_trigger="The talks have been postponed indefinitely.",
    )
    other = _signal(confirmed_location="pakistan")
    decision = classify_agreement(config, [ambiguous, other])
    assert decision.action == "ALERT_ONLY"
    assert "classifier_pass_disagreement" in decision.reason


def test_classify_agreement_empty_passes_is_alert_only() -> None:
    config = _config()
    decision = classify_agreement(config, [])
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "classifier_unavailable"


# ---- location market verifier ----


def _fake_market(
    *,
    label: str,
    question: str,
    slug: str,
    condition_id: str,
    yes_token: str,
    no_token: str,
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
) -> dict:
    return {
        "groupItemTitle": label,
        "question": question,
        "slug": slug,
        "conditionId": condition_id,
        "clobTokenIds": json.dumps([yes_token, no_token]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "negRisk": True,
    }


def _fake_event(markets: list[dict], description: str = "rules text") -> dict:
    return {
        "slug": "test-event-slug",
        "title": "Test event",
        "description": description,
        "markets": markets,
    }


def test_verify_location_event_matches_by_group_item_title(monkeypatch) -> None:
    config = _config()
    event = _fake_event(
        [
            _fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no"),
            _fake_market(label="Pakistan", question="P?", slug="pk-slug", condition_id="0xpk", yes_token="pk-yes", no_token="pk-no"),
        ]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    assert verification.outcomes["qatar"].found
    assert verification.outcomes["qatar"].condition_id_matches
    assert verification.outcomes["qatar"].yes_token_matches
    assert verification.outcomes["qatar"].no_token_matches
    assert verification.outcomes["qatar"].tradeable


def test_verify_location_event_flags_token_mismatch(monkeypatch) -> None:
    config = _config()
    event = _fake_event(
        [_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="WRONG-TOKEN", no_token="qatar-no")]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    assert verification.outcomes["qatar"].found
    assert not verification.outcomes["qatar"].yes_token_matches
    assert verification.outcomes["qatar"].mismatch_reason == "token_id_mismatch"


def test_verify_critical_outcomes_raises_on_missing_rotation_target(monkeypatch) -> None:
    config = _config()
    # Qatar (held) present and correct, but Pakistan (a rotation target) is
    # entirely missing from the live event -- must fail closed.
    event = _fake_event(
        [_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no")]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    with pytest.raises(ValueError, match="location market verification failed"):
        verify_critical_outcomes(config, verification)


def test_verify_location_event_raises_on_pinned_rule_hash_mismatch(monkeypatch) -> None:
    config = _config(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
            expected_rule_text_sha256="deadbeef" * 8,
        )
    )
    event = _fake_event([_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no")])
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    with pytest.raises(ValueError, match="rule text changed"):
        verify_location_event(config)


# ---- no_meeting ordering regression (found via smoke-location-classifier) ----


def test_no_meeting_realistic_llm_signal_still_exits() -> None:
    # A collapse report as the LLM actually emits it: there is no senior round,
    # so qualifies_as_senior_round is False and strength is "denied". Before the
    # ordering fix this fell into the senior-round gate and returned NO_ACTION.
    config = _config()
    decision = final_decision(
        config,
        _signal(
            confirmed_location="no_meeting",
            qualifies_as_senior_round=False,
            round_status="none",
            evidence_strength="denied",
            source_tier="wire",
            level="0",
        ),
    )
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_no_meeting_speculative_is_alert_only() -> None:
    config = _config()
    decision = final_decision(
        config,
        _signal(
            confirmed_location="no_meeting",
            qualifies_as_senior_round=False,
            round_status="rumor",
            evidence_strength="speculative",
            source_tier="other",
        ),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "no_meeting_reported_unconfirmed"


# ---- time-decay price floors ----


def _decay_config(**overrides) -> LocationBotConfig:
    return _config(
        time_decay=TimeDecayConfig(
            enabled=True,
            trim_after_date="2026-09-16",
            exit_after_date="2026-09-23",
            trim_fraction=0.25,
            min_trim_price=0.05,
            min_exit_price=0.10,
        ),
        **overrides,
    )


def test_time_decay_exit_blocked_below_price_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.04)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "TIME", "time_decay_exit")
    assert executor.execute(decision, article("time decay")) == "TIME_DECAY_PRICE_FLOOR"
    current = executor.store.current()
    assert current is not None and current.state == "TIME_DECAY_PRICE_FLOOR"
    # Second blocked cycle keeps the state but must not re-notify.
    assert executor.execute(decision, article("time decay again")) == "TIME_DECAY_PRICE_FLOOR"
    floor_notes = [n for n in executor._notified if "floor" in n[0]]  # type: ignore[attr-defined]
    assert len(floor_notes) == 1


def test_time_decay_trim_blocked_below_trim_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.04)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("TRIM_YES", "TIME", "time_decay_trim")
    assert executor.execute(decision, article("time decay")) == "TIME_DECAY_PRICE_FLOOR"


def test_time_decay_trim_proceeds_above_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.20)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("TRIM_YES", "TIME", "time_decay_trim")
    assert executor.execute(decision, article("time decay")) == "TRIMMED"


def test_news_triggered_exit_is_not_floored(tmp_path) -> None:
    # Floors only apply to calendar-decay sales; a confirmed adverse news
    # trigger must still exit even below the decay floor.
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.01)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    assert executor.execute(decision, article("No qualifying round will occur.")) == "EXITED"


# ---- runner: time-decay decisions must not re-execute/notify every cycle ----


def test_run_once_skips_decay_after_trim_and_exit(tmp_path) -> None:
    from polybot.location.runner import LocationProtectionBot

    config = _config(
        time_decay=TimeDecayConfig(enabled=True, trim_after_date="2020-01-01", exit_after_date="2099-01-01", trim_fraction=0.25),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.50)
    bot = LocationProtectionBot(config=config, adapter=adapter)
    first = bot.run_once()
    assert [d.action for d in first] == ["TRIM_YES"]
    current = bot.store.current()
    assert current is not None and current.state == "TRIMMED"
    # Subsequent cycles: trim already recorded, decay decision suppressed.
    assert bot.run_once() == []
    assert bot.run_once() == []
