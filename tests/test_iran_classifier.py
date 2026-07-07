from __future__ import annotations

from polybot.iran.classifier import RuleBasedFixtureClassifier
from polybot.gamma import MarketMeta
from datetime import date

from polybot.iran.config import ClassifierConfig, IranBotConfig, MarketConfig, SourcesConfig, TimeDecayConfig, TriggerConfig
from polybot.iran.decision import Decision, classify_agreement, final_decision, time_decay_decision, verify_quote_or_alert
from polybot.iran.executor import DryRunTradingAdapter
from polybot.iran.runner import IranProtectionBot, _active_scheduled_hold
from polybot.iran.source_fetcher import fetch_feed_articles
from polybot.iran.types import Article, SignalFactors


def article(text: str, domain: str = "reuters.com") -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title=text,
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((domain, text)))),
    )


def classify(text: str) -> SignalFactors:
    classifier = RuleBasedFixtureClassifier(SourcesConfig())
    return classifier.classify(article(text), "market rule text")


def test_technical_talks_are_level_1_no_trade() -> None:
    factors = classify("Technical talks continue after funeral between implementation teams.")
    assert factors.level == "1"
    assert factors.technical_or_implementation_only is True
    assert factors.protect_no_position is False


def test_mou_communication_channel_is_level_1_no_trade() -> None:
    factors = classify("A communication channel for MoU breaches opened through mediators.")
    assert factors.level == "1"
    assert factors.protect_no_position is False


def test_no_meeting_scheduled_is_level_1_no_trade() -> None:
    factors = classify("Iran says no meeting with the US is scheduled.")
    assert factors.level == "1"
    assert factors.protect_no_position is False


def test_negation_beats_breakdown_keywords() -> None:
    factors = classify("Iran denies reports of strikes and says negotiations have not broken down.")
    assert factors.level == "1"
    assert factors.recommended_action == "no_action"
    assert factors.event_type == "noise"


def test_broader_future_talks_are_level_3_alert_only() -> None:
    factors = classify("Qatar says talks may expand to broader issues later.")
    assert factors.level == "3"
    assert factors.recommended_action == "alert_only"


def test_formal_senior_round_scheduled_before_deadline_is_level_4a() -> None:
    factors = classify("Qatar confirms formal senior-level US-Iran peace talks scheduled for July 14 in Doha.")
    assert factors.level == "4A"
    assert factors.protect_no_position is True
    assert factors.recommended_action == "sell_no_and_buy_yes"


def test_named_principals_meeting_headline_reaches_scheduled_round() -> None:
    factors = classify("Araghchi and Witkoff to meet in Doha on July 14 for a new round of talks.")
    assert factors.level == "4A"
    assert factors.event_type == "round_scheduled"
    assert factors.seniority == "senior"
    assert factors.timing_relative_to_deadline == "before"


def test_formal_senior_round_after_deadline_does_not_trade() -> None:
    factors = classify("Formal senior-level US-Iran peace talks scheduled for August 5 in Doha.")
    assert factors.level == "3"
    assert factors.protect_no_position is False


def test_formal_senior_round_begun_before_deadline_is_level_4b() -> None:
    factors = classify("Reuters reports formal senior-level US-Iran peace talks have begun in Doha before July 17.")
    assert factors.level == "4B"
    assert factors.protect_no_position is True
    assert factors.would_resolve_yes_if_true is True


def test_one_reuters_source_can_execute_if_checks_pass() -> None:
    factors = classify("Reuters reports formal senior-level US-Iran peace talks scheduled for July 14 in Doha.")
    decision = classify_agreement([factors, factors])
    decision = verify_quote_or_alert(decision, "Reuters reports formal senior-level US-Iran peace talks scheduled for July 14 in Doha.")
    assert decision.action == "SELL_NO_CONDITIONAL_BUY_YES"


def test_quote_not_found_downgrades_to_level_3_alert() -> None:
    factors = classify("Reuters reports formal senior-level US-Iran peace talks scheduled for July 14 in Doha.")
    decision = verify_quote_or_alert(Decision("SELL_NO_CONDITIONAL_BUY_YES", "4A", "formal_round_scheduled", factors), "Different article body.")
    assert decision.action == "ALERT_ONLY"
    assert decision.level == "3"
    assert decision.reason == "quote_verification_failed"


def test_classifier_pass_disagreement_is_alert_only() -> None:
    first = classify("Reuters reports formal senior-level US-Iran peace talks scheduled for July 14 in Doha.")
    second = classify("Qatar says talks may expand to broader issues later.")
    decision = classify_agreement([first, second])
    assert decision.action == "ALERT_ONLY"
    assert decision.level == "3"
    assert decision.reason.startswith("classifier_pass_disagreement")


def test_yes_protection_senior_round_scheduled_before_deadline_is_hold_signal() -> None:
    factors = classify("Reuters reports senior US and Iranian representatives scheduled a formal round of talks for July 14.")
    decision = final_decision(
        SignalFactors(
            **{**factors.__dict__, "event_type": "round_scheduled", "seniority": "senior", "timing_relative_to_deadline": "before", "source_tier": "wire"}
        ),
        held_side="YES",
    )
    assert decision.action == "NO_ACTION"
    assert decision.reason == "senior_round_scheduled_hold_not_resolved"


def test_yes_protection_tier_one_postponed_after_deadline_exits() -> None:
    factors = SignalFactors(
        source_is_trusted=True,
        event_status="postponed",
        before_deadline=False,
        scheduled_before_july30=False,
        begun_before_july31=False,
        formal_senior_level_round=False,
        senior_us_representative_involved=False,
        senior_iran_representative_involved=False,
        in_person_or_indirect_in_person=False,
        peace_talks_or_negotiations=True,
        technical_or_implementation_only=False,
        protect_no_position=False,
        would_resolve_yes_if_true=False,
        recommended_action="sell_yes_and_buy_no",
        level="4B",
        quote_supporting_trigger="Reuters reports the next senior-level round was postponed until after July 17.",
        event_type="round_postponed",
        seniority="senior",
        timing_relative_to_deadline="after",
        source_tier="wire",
    )
    decision = final_decision(factors, held_side="YES")
    assert decision.action == "EXIT_YES_OPTIONAL_BUY_NO"


def test_yes_protection_weak_negative_signal_trims_only() -> None:
    factors = SignalFactors(
        source_is_trusted=True,
        event_status="postponed",
        before_deadline=False,
        scheduled_before_july30=False,
        begun_before_july31=False,
        formal_senior_level_round=False,
        senior_us_representative_involved=False,
        senior_iran_representative_involved=False,
        in_person_or_indirect_in_person=False,
        peace_talks_or_negotiations=True,
        technical_or_implementation_only=False,
        protect_no_position=False,
        would_resolve_yes_if_true=False,
        recommended_action="trim_yes",
        level="3",
        quote_supporting_trigger="A state media report says talks may be postponed.",
        event_type="round_postponed",
        seniority="unclear",
        timing_relative_to_deadline="unstated",
        source_tier="state_media",
    )
    decision = final_decision(factors, held_side="YES")
    assert decision.action == "TRIM_YES"


def test_yes_protection_unclear_seniority_never_full_exit() -> None:
    factors = SignalFactors(
        source_is_trusted=True,
        event_status="scheduled",
        before_deadline=True,
        scheduled_before_july30=True,
        begun_before_july31=False,
        formal_senior_level_round=False,
        senior_us_representative_involved=False,
        senior_iran_representative_involved=False,
        in_person_or_indirect_in_person=True,
        peace_talks_or_negotiations=True,
        technical_or_implementation_only=False,
        protect_no_position=False,
        would_resolve_yes_if_true=False,
        recommended_action="alert_only",
        level="3",
        quote_supporting_trigger="Talks were scheduled before July 17.",
        event_type="round_scheduled",
        seniority="unclear",
        timing_relative_to_deadline="before",
        source_tier="wire",
    )
    decision = final_decision(factors, held_side="YES")
    assert decision.action == "ALERT_ONLY"


def test_yes_time_decay_trim_and_exit() -> None:
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        time_decay=TimeDecayConfig(enabled=True, trim_after_date="2026-07-12", exit_after_date="2026-07-15", trim_fraction=0.25),
    )
    assert time_decay_decision(cfg, today=date(2026, 7, 11)).action == "NO_ACTION"
    assert time_decay_decision(cfg, today=date(2026, 7, 12)).action == "TRIM_YES"
    assert time_decay_decision(cfg, today=date(2026, 7, 15)).action == "EXIT_YES_ONLY"


def test_yes_scheduled_hold_state_suspends_time_decay(tmp_path) -> None:
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        time_decay=TimeDecayConfig(enabled=True, scheduled_signal_suspension_days=3),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = cfg.data_dir
    from polybot.iran.storage import StateStore

    record = StateStore(store).write("YES_SCHEDULED_HOLD_SIGNAL", reason="senior_round_scheduled_hold_not_resolved")
    assert _active_scheduled_hold(cfg, record) is not None


class TrustingClassifier:
    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        return SignalFactors(
            source_is_trusted=True,
            event_status="scheduled",
            before_deadline=True,
            scheduled_before_july30=True,
            begun_before_july31=False,
            formal_senior_level_round=True,
            senior_us_representative_involved=True,
            senior_iran_representative_involved=True,
            in_person_or_indirect_in_person=True,
            peace_talks_or_negotiations=True,
            technical_or_implementation_only=False,
            protect_no_position=True,
            would_resolve_yes_if_true=False,
            recommended_action="sell_no_and_buy_yes",
            level="4A",
            quote_supporting_trigger="Araghchi and Witkoff to meet in Doha on July 14.",
        )


class ScheduledHoldClassifier:
    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        return SignalFactors(
            source_is_trusted=True,
            event_status="scheduled",
            before_deadline=True,
            scheduled_before_july30=True,
            begun_before_july31=False,
            formal_senior_level_round=True,
            senior_us_representative_involved=True,
            senior_iran_representative_involved=True,
            in_person_or_indirect_in_person=True,
            peace_talks_or_negotiations=True,
            technical_or_implementation_only=False,
            protect_no_position=True,
            would_resolve_yes_if_true=False,
            recommended_action="sell_no_and_buy_yes",
            level="4A",
            quote_supporting_trigger="Araghchi and Witkoff to meet in Doha on July 14.",
            event_type="round_scheduled",
            seniority="senior",
            timing_relative_to_deadline="before",
            source_tier="wire",
        )


class FailingClassifier:
    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        raise AssertionError("classifier should not be called")


class ErrorClassifier:
    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        raise RuntimeError("classifier down")


class RecordingAdapter(DryRunTradingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def query_live_position(self, yes_token_id: str, no_token_id: str):
        self.calls.append("query_live_position")
        return super().query_live_position(yes_token_id, no_token_id)

    def cancel_open_orders_for_market(self, condition_id: str):
        self.calls.append("cancel")
        return super().cancel_open_orders_for_market(condition_id)

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float):
        self.calls.append("sell")
        return super().sell_no_fak(no_token_id, shares, min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float):
        self.calls.append("buy")
        return super().buy_yes_fak(yes_token_id, usd, max_price)


def test_untrusted_domain_cannot_execute_even_if_classifier_claims_trusted(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter)
    bot.classifier = ScheduledHoldClassifier()

    decision = bot.process_article(article("Araghchi and Witkoff to meet in Doha on July 14.", domain="rumor.example"))

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_domain_not_auto_trade"
    assert adapter.calls == []


def test_alert_only_domain_cannot_execute_even_if_classifier_claims_trusted(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter)
    bot.classifier = ScheduledHoldClassifier()

    decision = bot.process_article(article("Araghchi and Witkoff to meet in Doha on July 14.", domain="x.com"))

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_domain_alert_only"
    assert adapter.calls == []


def test_feed_item_does_not_auto_trade_by_default(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter)
    bot.classifier = FailingClassifier()
    feed_article = Article(
        url="https://news.google.com/rss/articles/example",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="feed-h",
        source_kind="feed",
    )

    decision = bot.process_article(feed_article)

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "feed_summary_classification_disabled"
    assert adapter.calls == []


def test_promoted_feed_summary_does_not_auto_trade(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter)
    bot.classifier = FailingClassifier()
    promoted_summary = Article(
        url="https://www.reuters.com/world/middle-east/story",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:01Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="summary-h",
        source_kind="promoted_feed_summary",
    )

    decision = bot.process_article(promoted_summary)

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "feed_summary_classification_disabled"
    assert adapter.calls == []


def test_feed_summary_skip_does_not_increment_classifier_budget(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter)
    bot.classifier = FailingClassifier()
    feed_article = Article(
        url="https://news.google.com/rss/articles/example",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="feed-budget-h",
        source_kind="feed",
    )

    decision = bot.process_article(feed_article)

    assert decision.reason == "feed_summary_classification_disabled"
    assert not (bot.store.data_dir / "classifier_budget.json").exists()


def test_feed_scheduled_hold_can_suspend_yes_time_decay_when_enabled(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter, held_side="YES", classifier=ClassifierConfig(classify_feed_summaries=True))
    bot.classifier = ScheduledHoldClassifier()
    feed_article = Article(
        url="https://news.google.com/rss/articles/example",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="feed-hold",
        source_kind="feed",
    )

    decision = bot.process_article(feed_article)

    assert decision.action == "NO_ACTION"
    assert decision.reason == "senior_round_scheduled_hold_not_resolved"
    state = bot.store.current()
    assert state is not None
    assert state.state == "YES_SCHEDULED_HOLD_SIGNAL"


def test_feed_scheduled_hold_does_not_overwrite_terminal_state(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter, held_side="YES", classifier=ClassifierConfig(classify_feed_summaries=True))
    bot.classifier = ScheduledHoldClassifier()
    bot.store.write("EXITED", reason="already_exited")
    feed_article = Article(
        url="https://news.google.com/rss/articles/example",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="feed-hold-terminal",
        source_kind="feed",
    )

    decision = bot.process_article(feed_article)

    assert decision.action == "NO_ACTION"
    state = bot.store.current()
    assert state is not None
    assert state.state == "EXITED"


def test_require_two_sources_downgrades_execution_to_alert(tmp_path) -> None:
    adapter = RecordingAdapter()
    bot = source_policy_bot(tmp_path, adapter, trigger=TriggerConfig(require_two_sources=True))
    bot.classifier = TrustingClassifier()

    decision = bot.process_article(article("Araghchi and Witkoff to meet in Doha on July 14."))

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "two_source_confirmation_not_implemented"
    assert adapter.calls == []


def test_classifier_hourly_cap_persists_across_bot_instances(tmp_path) -> None:
    classifier_config = ClassifierConfig(max_escalations_per_hour=1, max_escalations_per_day=10, max_classifier_errors_per_hour=10)
    trigger = TriggerConfig(require_two_sources=True)
    first = source_policy_bot(tmp_path, RecordingAdapter(), trigger=trigger, classifier=classifier_config)
    first.classifier = TrustingClassifier()

    first_decision = first.process_article(article("Araghchi and Witkoff to meet in Doha on July 14."))

    assert first_decision.reason == "two_source_confirmation_not_implemented"
    second = source_policy_bot(tmp_path, RecordingAdapter(), trigger=trigger, classifier=classifier_config)
    second.classifier = FailingClassifier()
    second_decision = second.process_article(article("Araghchi and Witkoff to meet in Doha on July 14. New item."))
    assert second_decision.action == "ALERT_ONLY"
    assert second_decision.reason == "classifier_budget_exhausted_hourly"


def test_classifier_daily_cap_persists_across_bot_instances(tmp_path) -> None:
    classifier_config = ClassifierConfig(max_escalations_per_hour=10, max_escalations_per_day=1, max_classifier_errors_per_hour=10)
    trigger = TriggerConfig(require_two_sources=True)
    first = source_policy_bot(tmp_path, RecordingAdapter(), trigger=trigger, classifier=classifier_config)
    first.classifier = TrustingClassifier()
    first.process_article(article("Araghchi and Witkoff to meet in Doha on July 14."))

    second = source_policy_bot(tmp_path, RecordingAdapter(), trigger=trigger, classifier=classifier_config)
    second.classifier = FailingClassifier()
    second_decision = second.process_article(article("Araghchi and Witkoff to meet in Doha on July 14. Another item."))

    assert second_decision.action == "ALERT_ONLY"
    assert second_decision.reason == "classifier_budget_exhausted_daily"


def test_classifier_error_cap_blocks_later_attempts(tmp_path) -> None:
    classifier_config = ClassifierConfig(max_escalations_per_hour=10, max_escalations_per_day=10, max_classifier_errors_per_hour=1)
    first = source_policy_bot(tmp_path, RecordingAdapter(), classifier=classifier_config)
    first.classifier = ErrorClassifier()

    first_decision = first.process_article(article("Araghchi and Witkoff to meet in Doha on July 14."))

    assert first_decision.reason.startswith("classifier_error:")
    second = source_policy_bot(tmp_path, RecordingAdapter(), classifier=classifier_config)
    second.classifier = FailingClassifier()
    second_decision = second.process_article(article("Araghchi and Witkoff to meet in Doha on July 14. Error cap item."))
    assert second_decision.action == "ALERT_ONLY"
    assert second_decision.reason == "classifier_error_cap_exceeded"


def test_one_pass_sonnet_mode_does_not_call_pass_agreement(tmp_path, monkeypatch) -> None:
    def fail_agreement(*args, **kwargs):
        raise AssertionError("classify_agreement should not be called")

    monkeypatch.setattr("polybot.iran.runner.classify_agreement", fail_agreement)
    classifier_config = ClassifierConfig(
        model="claude-sonnet-4-6",
        passes=1,
        require_pass_agreement=False,
        max_escalations_per_hour=10,
        max_escalations_per_day=10,
    )
    bot = source_policy_bot(tmp_path, RecordingAdapter(), held_side="YES", classifier=classifier_config)
    bot.classifier = ScheduledHoldClassifier()

    decision = bot.process_article(article("Araghchi and Witkoff to meet in Doha on July 14."))

    assert decision.action == "NO_ACTION"
    assert decision.reason == "senior_round_scheduled_hold_not_resolved"


def test_stale_article_skips_classifier_before_budget_increment(tmp_path) -> None:
    bot = source_policy_bot(tmp_path, RecordingAdapter())
    bot.classifier = FailingClassifier()
    stale = Article(
        url="https://apnews.com/old-strikes-story",
        domain="apnews.com",
        title="Iran attacks following US strikes",
        published_at="Mon, 29 Jun 2026 05:15:00 GMT",
        fetched_at="2026-07-05T12:00:00Z",
        raw_text="Iran attacked following US strikes.",
        hash="stale-budget-h",
        source_kind="article",
    )

    decision = bot.process_article(stale)

    assert decision.reason.startswith("article_stale_skipped_classification")
    assert not (bot.store.data_dir / "classifier_budget.json").exists()


def test_fetch_feed_articles_uses_rss_source_domain(monkeypatch) -> None:
    class FakeResponse:
        content = b"""<?xml version="1.0"?>
<rss><channel><item>
<title>Araghchi and Witkoff to meet in Doha on July 14</title>
<link>https://news.google.com/rss/articles/example</link>
<source url="https://www.reuters.com">Reuters</source>
<pubDate>Sat, 04 Jul 2026 12:00:00 GMT</pubDate>
<description>Senior US-Iran talks are scheduled.</description>
</item></channel></rss>"""

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("polybot.iran.source_fetcher.requests.get", lambda *args, **kwargs: FakeResponse())

    articles = fetch_feed_articles("https://example.com/feed", include_terms=["witkoff"], limit=10)

    assert len(articles) == 1
    assert articles[0].domain == "reuters.com"
    assert articles[0].source_kind == "feed"
    assert articles[0].url == "https://news.google.com/rss/articles/example"


def test_fetch_feed_articles_filters_excluded_noise(monkeypatch) -> None:
    class FakeResponse:
        content = b"""<?xml version="1.0"?>
<rss><channel><item>
<title>U.S. Visa: Reciprocity and Civil Documents by Country</title>
<link>https://travel.state.gov/content/travel/example</link>
<source url="https://travel.state.gov">State</source>
<description>Iran visa reciprocity documents.</description>
</item></channel></rss>"""

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("polybot.iran.source_fetcher.requests.get", lambda *args, **kwargs: FakeResponse())

    articles = fetch_feed_articles("https://example.com/feed", include_terms=["iran"], exclude_terms=["visa", "travel.state.gov"])

    assert articles == []


def test_fetch_feed_articles_ignores_html_error_page(monkeypatch) -> None:
    class FakeResponse:
        content = b"<!DOCTYPE html><html><title>Technical Difficulties</title></html>"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("polybot.iran.source_fetcher.requests.get", lambda *args, **kwargs: FakeResponse())

    assert fetch_feed_articles("https://www.state.gov/rss-feed/press-releases/feed/", include_terms=["iran"]) == []


def test_run_once_promotes_feed_item_to_full_article_hold_signal(tmp_path, monkeypatch) -> None:
    adapter = RecordingAdapter()
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event"),
        sources=SourcesConfig(
            auto_trade_domains=["reuters.com"],
            alert_only_domains=["x.com"],
            feed_urls=["https://feeds.example/reuters"],
            allow_feed_auto_trade=False,
            promote_feed_to_article=True,
            max_trade_article_age_hours=10_000,
        ),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    bot = IranProtectionBot(
        config=cfg,
        market=MarketMeta(
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
        ),
        market_rule_text="rules",
        adapter=adapter,
    )
    bot.classifier = ScheduledHoldClassifier()
    feed_article = Article(
        url="https://www.reuters.com/world/middle-east/story",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:00Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="feed-promote",
        source_kind="feed",
    )
    promoted_article = Article(
        url="https://www.reuters.com/world/middle-east/story",
        domain="reuters.com",
        title="Araghchi and Witkoff to meet in Doha on July 14.",
        published_at=None,
        fetched_at="2026-07-03T00:00:01Z",
        raw_text="Araghchi and Witkoff to meet in Doha on July 14.",
        hash="promoted-promote",
        source_kind="article",
    )
    monkeypatch.setattr("polybot.iran.runner.fetch_feed_articles", lambda *args, **kwargs: [feed_article])
    monkeypatch.setattr("polybot.iran.runner.promote_feed_article", lambda *args, **kwargs: promoted_article)

    decisions = bot.run_once()

    assert [decision.reason for decision in decisions] == [
        "feed_summary_classification_disabled",
        "senior_round_scheduled_hold_not_resolved",
    ]
    assert adapter.calls == []
    state = bot.store.current()
    assert state is not None
    assert state.state == "YES_SCHEDULED_HOLD_SIGNAL"


def source_policy_bot(
    tmp_path,
    adapter: RecordingAdapter,
    trigger: TriggerConfig | None = None,
    held_side: str = "NO",
    classifier: ClassifierConfig | None = None,
) -> IranProtectionBot:
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side=held_side),
        trigger=trigger or TriggerConfig(),
        classifier=classifier or ClassifierConfig(),
        sources=SourcesConfig(auto_trade_domains=["reuters.com"], alert_only_domains=["x.com", "twitter.com", "t.me"]),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    return IranProtectionBot(
        config=cfg,
        market=MarketMeta(
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
        ),
        market_rule_text="rules",
        adapter=adapter,
    )


class _FakeAnthropicBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.stop_reason = stop_reason
        self.content = [_FakeAnthropicBlock("thinking"), _FakeAnthropicBlock("text", text)]


class _FakeAnthropicMessages:
    def __init__(self, response: _FakeAnthropicResponse) -> None:
        self.response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class _FakeAnthropicClient:
    def __init__(self, response: _FakeAnthropicResponse) -> None:
        self.messages = _FakeAnthropicMessages(response)


def _anthropic_classifier(response: _FakeAnthropicResponse) -> "LLMClassifier":
    from polybot.iran.classifier import LLMClassifier
    from polybot.iran.config import ClassifierConfig

    config = ClassifierConfig(provider="anthropic", model="claude-opus-4-8")
    return LLMClassifier(config, SourcesConfig(), anthropic_client=_FakeAnthropicClient(response))


def test_anthropic_classifier_parses_structured_json() -> None:
    import json

    payload = {
        "source_is_trusted": True,
        "event_status": "scheduled",
        "before_deadline": True,
        "scheduled_before_july30": True,
        "begun_before_july31": False,
        "formal_senior_level_round": True,
        "senior_us_representative_involved": True,
        "senior_iran_representative_involved": True,
        "in_person_or_indirect_in_person": True,
        "peace_talks_or_negotiations": True,
        "technical_or_implementation_only": False,
        "protect_no_position": True,
        "would_resolve_yes_if_true": False,
        "recommended_action": "no_action",
        "level": "4A",
        "quote_supporting_trigger": "A new round of talks is scheduled for July 10 in Doha.",
        "event_type": "round_scheduled",
        "seniority": "senior",
        "timing_relative_to_deadline": "before",
        "source_tier": "wire",
    }
    classifier = _anthropic_classifier(_FakeAnthropicResponse(json.dumps(payload)))
    factors = classifier.classify(article("A new round of talks is scheduled for July 10 in Doha."), "rule text")
    assert factors.event_type == "round_scheduled"
    assert factors.level == "4A"
    assert factors.seniority == "senior"
    sent = classifier._anthropic_client.messages.last_kwargs
    assert sent["model"] == "claude-opus-4-8"
    assert "temperature" not in sent
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_anthropic_classifier_refusal_raises() -> None:
    import pytest

    classifier = _anthropic_classifier(_FakeAnthropicResponse("", stop_reason="refusal"))
    with pytest.raises(RuntimeError, match="refused"):
        classifier.classify(article("A new round of talks is scheduled."), "rule text")


def _claude_cli_classifier(cli_runner) -> "LLMClassifier":
    from polybot.iran.classifier import LLMClassifier
    from polybot.iran.config import ClassifierConfig

    config = ClassifierConfig(provider="claude_cli", model="claude-sonnet-4-6")
    return LLMClassifier(config, SourcesConfig(), cli_runner=cli_runner)


def test_claude_cli_classifier_parses_result_field() -> None:
    import json

    payload = {
        "source_is_trusted": True,
        "event_status": "scheduled",
        "before_deadline": True,
        "scheduled_before_july30": True,
        "begun_before_july31": False,
        "formal_senior_level_round": True,
        "senior_us_representative_involved": True,
        "senior_iran_representative_involved": True,
        "in_person_or_indirect_in_person": True,
        "peace_talks_or_negotiations": True,
        "technical_or_implementation_only": False,
        "protect_no_position": True,
        "would_resolve_yes_if_true": False,
        "recommended_action": "no_action",
        "level": "4A",
        "quote_supporting_trigger": "A new round of talks is scheduled for July 10 in Doha.",
        "event_type": "round_scheduled",
        "seniority": "senior",
        "timing_relative_to_deadline": "before",
        "source_tier": "wire",
    }
    cli_envelope = json.dumps({"type": "result", "is_error": False, "result": json.dumps(payload), "total_cost_usd": 0.0})
    received_prompts: list[str] = []

    def fake_runner(prompt: str) -> str:
        received_prompts.append(prompt)
        return cli_envelope

    classifier = _claude_cli_classifier(fake_runner)
    factors = classifier.classify(article("A new round of talks is scheduled for July 10 in Doha."), "rule text")
    assert factors.event_type == "round_scheduled"
    assert factors.level == "4A"
    assert len(received_prompts) == 1
    assert classifier.last_usage == {"total_cost_usd": 0.0}


def test_claude_cli_classifier_prefers_structured_output_field() -> None:
    import json

    payload = {
        "source_is_trusted": True,
        "event_status": "held",
        "before_deadline": True,
        "scheduled_before_july30": True,
        "begun_before_july31": True,
        "formal_senior_level_round": True,
        "senior_us_representative_involved": True,
        "senior_iran_representative_involved": True,
        "in_person_or_indirect_in_person": True,
        "peace_talks_or_negotiations": True,
        "technical_or_implementation_only": False,
        "protect_no_position": True,
        "would_resolve_yes_if_true": True,
        "recommended_action": "sell_no_and_buy_yes",
        "level": "4B",
        "quote_supporting_trigger": "Talks began today in Doha.",
        "event_type": "round_occurred",
        "seniority": "senior",
        "timing_relative_to_deadline": "before",
        "source_tier": "wire",
    }
    cli_envelope = json.dumps({"type": "result", "is_error": False, "result": "see structured_output", "structured_output": payload})
    classifier = _claude_cli_classifier(lambda prompt: cli_envelope)
    factors = classifier.classify(article("Talks began today in Doha."), "rule text")
    assert factors.event_type == "round_occurred"
    assert factors.level == "4B"


def test_claude_cli_classifier_error_envelope_raises() -> None:
    import json

    import pytest

    classifier = _claude_cli_classifier(lambda prompt: json.dumps({"type": "result", "is_error": True, "result": "boom"}))
    with pytest.raises(RuntimeError, match="claude CLI reported an error"):
        classifier.classify(article("A new round of talks is scheduled."), "rule text")


def test_claude_cli_missing_binary_raises_actionable_error(monkeypatch) -> None:
    import pytest

    from polybot.iran.classifier import LLMClassifier
    from polybot.iran.config import ClassifierConfig

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("polybot.iran.classifier.subprocess.run", fake_run)
    config = ClassifierConfig(provider="claude_cli", model="claude-sonnet-4-6")
    classifier = LLMClassifier(config, SourcesConfig())
    with pytest.raises(RuntimeError, match="claude login"):
        classifier.classify(article("A new round of talks is scheduled."), "rule text")


def test_promote_google_news_item_uses_resolved_publisher_url(monkeypatch) -> None:
    from polybot.iran import source_fetcher

    feed_item = Article(
        url="https://news.google.com/rss/articles/CBMabc123",
        domain="reuters.com",
        title="US, Iran talks postponed past July 17 - Reuters",
        published_at=None,
        fetched_at="2026-07-04T00:00:00Z",
        raw_text="US, Iran talks postponed past July 17 - Reuters. Senior negotiators delayed the next round.",
        hash="feedhash1",
        source_kind="feed",
    )
    monkeypatch.setattr(
        source_fetcher, "resolve_google_news_url",
        lambda url, ua, timeout=20.0: "https://www.reuters.com/world/talks-postponed/",
    )

    def blocked_fetch(url, ua):
        import requests
        raise requests.RequestException("401 Client Error")

    monkeypatch.setattr(source_fetcher, "fetch_article", blocked_fetch)
    promoted = source_fetcher.promote_feed_article(feed_item, "ua")
    assert promoted is not None
    assert promoted.domain == "reuters.com"
    assert promoted.url == "https://www.reuters.com/world/talks-postponed/"
    assert promoted.source_kind == "promoted_feed_summary"
    assert "postponed past July 17" in promoted.raw_text


def test_promote_first_party_full_text_feed_as_article_when_publisher_blocks(monkeypatch) -> None:
    from polybot.iran import source_fetcher

    body = " ".join(["Senior diplomats will meet in Doha for formal talks."] * 25)
    feed_item = Article(
        url="https://www.dawn.com/news/2013507/pakistan-taking-on-mantle-of-mediation",
        domain="dawn.com",
        title="Dawn full text item",
        published_at="Tue, 07 Jul 2026 07:24:14 +0500",
        fetched_at="2026-07-07T00:00:00Z",
        raw_text=body,
        hash="dawn-feedhash",
        source_kind="feed",
    )

    def blocked_fetch(url, ua):
        import requests
        raise requests.RequestException("403 Client Error")

    monkeypatch.setattr(source_fetcher, "fetch_article", blocked_fetch)
    promoted = source_fetcher.promote_feed_article(feed_item, "ua")
    assert promoted is not None
    assert promoted.domain == "dawn.com"
    assert promoted.source_kind == "article"
    assert "formal talks" in promoted.raw_text


def test_promote_google_news_item_unresolvable_returns_none(monkeypatch) -> None:
    from polybot.iran import source_fetcher

    feed_item = Article(
        url="https://news.google.com/rss/articles/CBMabc123",
        domain="reuters.com",
        title="headline",
        published_at=None,
        fetched_at="2026-07-04T00:00:00Z",
        raw_text="headline text",
        hash="feedhash2",
        source_kind="feed",
    )
    monkeypatch.setattr(source_fetcher, "resolve_google_news_url", lambda url, ua, timeout=20.0: None)
    assert source_fetcher.promote_feed_article(feed_item, "ua") is None


def test_scheduled_hold_marker_survives_state_overwrite(tmp_path) -> None:
    from polybot.iran.storage import StateStore

    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        time_decay=TimeDecayConfig(enabled=True, scheduled_signal_suspension_days=3),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = StateStore(cfg.data_dir)
    store.write("YES_SCHEDULED_HOLD_SIGNAL", reason="senior_round_scheduled_hold_not_resolved")
    store.write("TIME_DECAY_PRICE_FLOOR", reason="yes_bid_below_time_decay_floor")
    # Current state no longer carries the hold, but the marker does.
    assert _active_scheduled_hold(cfg, store.current()) is None
    marker = store.marker("YES_SCHEDULED_HOLD_SIGNAL")
    assert marker is not None
    assert _active_scheduled_hold(cfg, marker) is not None


def test_pass_agreement_ignores_pure_verdict_fields() -> None:
    base = dict(
        source_is_trusted=True,
        event_status="postponed",
        before_deadline=False,
        scheduled_before_july30=False,
        begun_before_july31=False,
        formal_senior_level_round=True,
        senior_us_representative_involved=True,
        senior_iran_representative_involved=True,
        in_person_or_indirect_in_person=True,
        peace_talks_or_negotiations=True,
        technical_or_implementation_only=False,
        protect_no_position=False,
        would_resolve_yes_if_true=False,
        quote_supporting_trigger="The next round of negotiations has been postponed until August 4",
        event_type="round_postponed",
        seniority="senior",
        timing_relative_to_deadline="after",
        source_tier="wire",
    )
    # LLM passes agree on every YES-decision-relevant fact but jitter on the
    # verdict fields and on NO-thesis fields a YES holder never consumes.
    first = SignalFactors(**base, recommended_action="alert_only", level="2")
    second_fields = dict(base, protect_no_position=True, would_resolve_yes_if_true=True, formal_senior_level_round=False, before_deadline=True)
    second = SignalFactors(**second_fields, recommended_action="sell_yes_and_buy_no", level="4A")
    decision = classify_agreement([first, second], held_side="YES")
    assert decision.action == "EXIT_YES_OPTIONAL_BUY_NO"
    assert decision.level == "4B"


def test_stale_article_trade_action_downgraded_to_alert(tmp_path) -> None:
    from polybot.iran.runner import _article_age_hours

    stale = Article(
        url="https://apnews.com/old-strikes-story",
        domain="apnews.com",
        title="Iran attacks following US strikes",
        published_at="Mon, 29 Jun 2026 05:15:00 GMT",
        fetched_at="2026-07-05T12:00:00Z",
        raw_text="Iran attacked following US strikes.",
        hash="stalehash",
        source_kind="article",
    )
    age = _article_age_hours(stale)
    assert age is not None and age > 24

    fresh_iso = Article(
        url="https://apnews.com/fresh",
        domain="apnews.com",
        title="fresh",
        published_at="2026-07-05T11:00:00+00:00",
        fetched_at="2026-07-05T12:00:00Z",
        raw_text="fresh",
        hash="freshhash",
    )
    assert _article_age_hours(fresh_iso) is not None

    unknown = Article(
        url="https://apnews.com/unknown",
        domain="apnews.com",
        title="unknown",
        published_at=None,
        fetched_at="2026-07-05T12:00:00Z",
        raw_text="unknown",
        hash="unknownhash",
    )
    assert _article_age_hours(unknown) is None


def test_source_policy_blocks_stale_trade_actions(tmp_path) -> None:
    from polybot.gamma import MarketMeta
    from polybot.iran.executor import DryRunTradingAdapter
    from polybot.iran.runner import IranProtectionBot

    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    market = MarketMeta(
        event_slug="iran-event", market_slug="july-17", condition_id="0xc", question="July 17?",
        description="d", resolution_source="", outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
        yes_token_id="1", no_token_id="2", tick_size="0.01", neg_risk=False,
        active=True, closed=False, accepting_orders=True, volume=0.0, liquidity=0.0,
    )
    bot = IranProtectionBot(config=cfg, market=market, market_rule_text="rules", adapter=DryRunTradingAdapter())
    stale = Article(
        url="https://apnews.com/old", domain="apnews.com", title="old",
        published_at="Mon, 29 Jun 2026 05:15:00 GMT", fetched_at="2026-07-05T12:00:00Z",
        raw_text="old", hash="h1",
    )
    decision = bot._enforce_source_policy(stale, Decision("EXIT_YES_OPTIONAL_BUY_NO", "4B", "strikes_or_breakdown"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("article_stale_for_auto_trade")

    fresh = Article(
        url="https://apnews.com/new", domain="apnews.com", title="new",
        published_at=None, fetched_at="2026-07-05T12:00:00Z", raw_text="new", hash="h2",
    )
    decision = bot._enforce_source_policy(fresh, Decision("EXIT_YES_OPTIONAL_BUY_NO", "4B", "strikes_or_breakdown"))
    assert decision.action == "EXIT_YES_OPTIONAL_BUY_NO"


def test_text_extractor_prefers_article_body_over_chrome() -> None:
    from polybot.iran.source_fetcher import _TextExtractor

    body = "Doha talks between senior negotiators resumed on Thursday. " * 12
    html_page = (
        "<html><head><title>Talks resume | AP News</title></head><body>"
        "<nav>Menu World SECTIONS Iran war Movies Fashion Television</nav>"
        "<header>Newsletters The Morning Wire</header>"
        f"<main><article><p>{body}</p></article></main>"
        "<footer>See All Newsletters Entertainment SECTIONS</footer>"
        "</body></html>"
    )
    parser = _TextExtractor()
    parser.feed(html_page)
    text = parser.text()
    assert "Doha talks between senior negotiators" in text
    assert "Movies Fashion" not in text
    assert "Newsletters" not in text


def test_text_extractor_falls_back_without_main_region() -> None:
    from polybot.iran.source_fetcher import _TextExtractor

    parser = _TextExtractor()
    parser.feed("<html><body><div>Short piece about talks.</div></body></html>")
    assert "Short piece about talks." in parser.text()


def test_telegram_notifier_never_raises_on_error_field_collision(monkeypatch) -> None:
    import requests as requests_module
    from polybot.iran.notifier import TelegramNotifier

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def failing_post(*args, **kwargs):
        raise requests_module.RequestException("400 Bad Request: chat not found")

    monkeypatch.setattr("polybot.iran.notifier.requests.post", failing_post)
    notifier = TelegramNotifier()
    # Caller passes error= (as runner does) while delivery fails: must not raise.
    notifier.notify("cycle failed; continuing", error="boom", event="collide")


def test_telegram_notifier_redacts_token_from_delivery_errors(monkeypatch) -> None:
    import requests as requests_module
    from polybot.iran.notifier import TelegramNotifier

    records = []
    token = "123456:secret-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def fake_log_event(event: str, **fields):
        records.append((event, fields))

    def failing_post(*args, **kwargs):
        raise requests_module.RequestException(f"400 Bad Request for https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr("polybot.iran.notifier.log_event", fake_log_event)
    monkeypatch.setattr("polybot.iran.notifier.requests.post", failing_post)

    TelegramNotifier().notify("cycle failed")

    notify_errors = [fields for event, fields in records if event == "iran_notify_error"]
    assert notify_errors
    assert token not in notify_errors[-1]["error"]
    assert "<redacted>" in notify_errors[-1]["error"]
