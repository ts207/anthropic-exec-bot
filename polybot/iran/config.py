from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
class MarketConfig:
    slug: str
    target_leg: str = "July 17"
    held_side: str = "YES"
    expected_question_contains: str = "July 17"
    expected_rule_text_sha256: str | None = None


@dataclass(frozen=True)
class PositionConfig:
    source: str = "onchain"
    expected_yes_token_id: str = ""
    expected_no_token_id: str = ""
    max_no_shares_to_sell: float = 100000.0
    max_yes_shares_to_sell: float = 100000.0
    max_yes_usd_to_buy: float = 100.0
    max_no_usd_to_buy: float = 100.0


@dataclass(frozen=True)
class TriggerConfig:
    auto_execute_level: int = 4
    require_two_sources: bool = False
    trusted_single_source_execution: bool = True


@dataclass(frozen=True)
class ClassifierConfig:
    provider: str = "rule_based"
    model: str = "rule_based_v1"
    temperature: float = 0.0
    passes: int = 2
    require_pass_agreement: bool = True
    require_verbatim_quote: bool = True
    include_market_rule_text: bool = True
    if_api_down: str = "urgent_alert_no_trade"


@dataclass(frozen=True)
class SellNoConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0


@dataclass(frozen=True)
class BuyYesConfig:
    enabled: bool = True
    max_price_level4a: float = 0.90
    max_price_level4b: float = 0.95
    usd_budget: float = 100.0
    skip_if_above_cap: bool = True


@dataclass(frozen=True)
class SellYesConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0
    trim_fraction: float = 0.25


@dataclass(frozen=True)
class BuyNoConfig:
    enabled: bool = True
    max_price_exit: float = 0.90
    usd_budget: float = 100.0
    skip_if_above_cap: bool = True


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    order_type: str = "FAK"
    sell_no: SellNoConfig = field(default_factory=SellNoConfig)
    buy_yes: BuyYesConfig = field(default_factory=BuyYesConfig)
    sell_yes: SellYesConfig = field(default_factory=SellYesConfig)
    buy_no: BuyNoConfig = field(default_factory=BuyNoConfig)


@dataclass(frozen=True)
class TimeDecayConfig:
    enabled: bool = False
    trim_after_date: str = ""
    exit_after_date: str = ""
    trim_fraction: float = 0.25
    suspend_exit_on_scheduled_signal: bool = True
    scheduled_signal_suspension_days: int = 3
    min_trim_price: float = 0.0
    min_exit_price: float = 0.0


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
    # Trade actions are downgraded to ALERT_ONLY for articles published more
    # than this many hours ago (0 disables). Alerts and hold signals are
    # unaffected.
    max_trade_article_age_hours: float = 24.0


@dataclass(frozen=True)
class IranBotConfig:
    market: MarketConfig
    position: PositionConfig = field(default_factory=PositionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    time_decay: TimeDecayConfig = field(default_factory=TimeDecayConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    data_dir: Path = Path("data/iran-protection-bot")
    logs_dir: Path = Path("logs")


def load_iran_config(path: Path) -> IranBotConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return IranBotConfig(
        market=MarketConfig(**_section(raw, "market")),
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        execution=ExecutionConfig(
            **{
                key: value
                for key, value in _section(raw, "execution").items()
                if key not in {"sell_no", "buy_yes", "sell_yes", "buy_no"}
            },
            sell_no=SellNoConfig(**_section(_section(raw, "execution"), "sell_no")),
            buy_yes=BuyYesConfig(**_section(_section(raw, "execution"), "buy_yes")),
            sell_yes=SellYesConfig(**_section(_section(raw, "execution"), "sell_yes")),
            buy_no=BuyNoConfig(**_section(_section(raw, "execution"), "buy_no")),
        ),
        time_decay=TimeDecayConfig(**_section(raw, "time_decay")),
        safety=SafetyConfig(**_section(raw, "safety")),
        sources=SourcesConfig(**_section(raw, "sources")),
        data_dir=Path(str(raw.get("data_dir", "data/iran-protection-bot"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
