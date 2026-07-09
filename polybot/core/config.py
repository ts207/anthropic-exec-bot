from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_AUTO_TRADE_DOMAINS = [
    "reuters.com",
    "apnews.com",
    "afp.com",
    "state.gov",
    "whitehouse.gov",
    "mfa.gov.ir",
    "mofa.gov.qa",
    "fm.gov.om",
    "mofa.gov.pk",
]

DEFAULT_ALERT_ONLY_DOMAINS = ["x.com", "twitter.com", "t.me", "irna.ir"]


@dataclass(frozen=True)
class ClassifierConfig:
    provider: str = "rule_based"
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.0
    passes: int = 1
    require_pass_agreement: bool = False
    require_verbatim_quote: bool = True
    include_market_rule_text: bool = True
    if_api_down: str = "urgent_alert_no_trade"
    max_escalations_per_hour: int = 4
    max_escalations_per_day: int = 20
    max_classifier_errors_per_hour: int = 3
    classify_feed_summaries: bool = False
    cli_binary: str = "claude"
    cli_timeout_seconds: int = 180


@dataclass(frozen=True)
class SafetyConfig:
    one_shot: bool = True
    cancel_open_orders_first: bool = True
    query_live_position: bool = True
    verify_fills_before_final_lock: bool = True
    quote_must_match_article_text: bool = True
    token_mapping_must_match: bool = True
    yes_cap_never_blocks_no_sell: bool = True
    degraded_mode_alert: bool = True
    max_executions: int = 1
    poll_seconds: float = 30.0


@dataclass(frozen=True)
class SourcesConfig:
    auto_trade_domains: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_TRADE_DOMAINS))
    alert_only_domains: list[str] = field(default_factory=lambda: list(DEFAULT_ALERT_ONLY_DOMAINS))
    poll_urls: list[str] = field(default_factory=list)
    feed_urls: list[str] = field(default_factory=list)
    feed_include_terms: list[str] = field(
        default_factory=lambda: [
            "iran",
            "u.s.",
            "us ",
            "united states",
            "witkoff",
            "araghchi",
            "doha",
            "oman",
            "qatar",
            "talks",
            "negotiations",
        ]
    )
    feed_exclude_terms: list[str] = field(
        default_factory=lambda: [
            "visa",
            "visas",
            "reciprocity",
            "civil documents",
            "travel advisory",
            "travel.state.gov",
            "aoprals.state.gov",
            "2001-2009.state.gov",
            "2009-2017.state.gov",
            "2021-2025.state.gov",
            "consular",
            "passport",
            "allowances",
        ]
    )
    max_feed_entries_per_cycle: int = 20
    allow_feed_auto_trade: bool = False
    promote_feed_to_article: bool = True
    max_trade_article_age_hours: float = 24.0
    allow_unknown_age_poll_auto_trade: bool = False

__all__ = [
    "ClassifierConfig",
    "DEFAULT_ALERT_ONLY_DOMAINS",
    "DEFAULT_AUTO_TRADE_DOMAINS",
    "SafetyConfig",
    "SourcesConfig",
]
