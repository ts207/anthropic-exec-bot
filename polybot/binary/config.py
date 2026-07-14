from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Reused as-is, same rationale as polybot.location.config: these shapes are
# provider/market-agnostic and OperatorGate duck-types on them.
from polybot.core.config import ClassifierConfig, SafetyConfig, SourcesConfig  # noqa: F401
from polybot.core.portfolio import PortfolioConfig  # noqa: F401

HELD_SIDES = {"", "YES", "NO"}
ENTRY_SIDES = {"YES", "NO"}


@dataclass(frozen=True)
class MarketConfig:
    """One binary (YES/NO) Polymarket market, defined entirely by config.

    The classifier judges every article strictly against `resolution_rules`,
    so pointing the bot at a new geopolitics market means pasting that
    market's verbatim rules here -- no code changes.
    """

    slug: str
    deadline_date: str  # ISO date the market resolves by
    question: str = ""
    # Substring that must appear in the live market question/slug; guards
    # against the slug resolving to a different leg (see gamma.select_market).
    expected_question_contains: str = ""
    # "" = start flat (requires entry.enabled); "YES"/"NO" = initial holding.
    # Once the bot enters/flips/exits, HoldingsStore overrides this value.
    held_side: str = ""
    resolution_rules: str = ""  # verbatim market resolution text, fed to the classifier
    analyst_context: str = ""  # user's own thesis/background, prior context only
    # Opt-in pin, same pattern as the iran/location verifiers: blank means
    # "not yet reviewed", not "verified".
    expected_rule_text_sha256: str = ""


@dataclass(frozen=True)
class PositionConfig:
    expected_yes_token_id: str = ""
    expected_no_token_id: str = ""
    max_shares_to_sell: float = 100000.0
    max_flip_usd_to_buy: float = 500.0
    # Sell the whole position when the held side's bid reaches this price:
    # once the thesis is priced in, the last few cents are not worth carrying
    # full resolution risk. 0 disables.
    take_profit_price: float = 0.0


@dataclass(frozen=True)
class EntryConfig:
    """Automated flat-to-position entry on rule-qualifying news.

    side "YES": buy YES when a trusted tier-one source confirms a qualifying
    event scheduled/underway/occurred before the deadline. side "NO": buy NO
    when a trusted tier-one source confirms the YES criteria are foreclosed
    (cancellation / cannot happen by the deadline).
    """

    enabled: bool = False
    side: str = "YES"
    usd_budget: float = 100.0
    max_price: float = 0.90
    # Balances at or below this are dust for wallet reconciliation.
    reconcile_min_shares: float = 0.01
    # Entries above this notional require a second independent source to have
    # confirmed the same thesis within the window before buying (0 = off).
    second_source_above_usd: float = 0.0
    second_source_window_minutes: float = 60.0
    # Post-entry corroboration: if no second independent source confirms the
    # thesis within this many minutes of entry, alert (or trim). 0 = off.
    corroboration_minutes: float = 0.0
    corroboration_action: str = "alert"  # alert | trim
    # Lifetime cap on entry executions for this position config.
    max_entries: int = 1


@dataclass(frozen=True)
class TriggerConfig:
    auto_execute_level: int = 4
    trusted_single_source_execution: bool = True


@dataclass(frozen=True)
class SellConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0
    trim_fraction: float = 0.25
    # Staged defense exits: < 1.0 sells this fraction first, requotes after
    # retry_delay_seconds, then sells the remainder -- softer on thin books.
    max_fraction_per_order: float = 1.0


@dataclass(frozen=True)
class FlipBuyConfig:
    """Optional opposite-side buy after a news-triggered exit of the held
    side (e.g. sell NO then buy YES once the qualifying event is confirmed).
    Disabled by default: exits are sell-only unless explicitly armed."""

    enabled: bool = False
    max_price: float = 0.95
    usd_budget: float = 500.0


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    sell: SellConfig = field(default_factory=SellConfig)
    flip_buy: FlipBuyConfig = field(default_factory=FlipBuyConfig)


@dataclass(frozen=True)
class TimeDecayConfig:
    enabled: bool = False
    trim_after_date: str = ""
    exit_after_date: str = ""
    trim_fraction: float = 0.25
    min_trim_price: float = 0.0
    min_exit_price: float = 0.0


@dataclass(frozen=True)
class KeywordsConfig:
    # Cheap deterministic pre-filter: only articles containing at least one of
    # these terms are escalated to the classifier. Empty = escalate everything
    # (the classifier budget still caps spend).
    escalate_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BinaryBotConfig:
    market: MarketConfig
    position: PositionConfig = field(default_factory=PositionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    time_decay: TimeDecayConfig = field(default_factory=TimeDecayConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    data_dir: Path = Path("data/binary-rule-bot")
    logs_dir: Path = Path("logs")


def load_binary_config(path: Path) -> BinaryBotConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML object")
    execution_raw = _section(raw, "execution")
    market_raw = dict(_section(raw, "market"))
    market_raw["held_side"] = str(market_raw.get("held_side", "") or "").strip().upper()
    entry_raw = dict(_section(raw, "entry"))
    if "side" in entry_raw:
        entry_raw["side"] = str(entry_raw["side"] or "").strip().upper()
    config = BinaryBotConfig(
        market=MarketConfig(**market_raw),
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        entry=EntryConfig(**entry_raw),
        portfolio=PortfolioConfig(**_section(raw, "portfolio")),
        execution=ExecutionConfig(
            dry_run=bool(execution_raw.get("dry_run", True)),
            sell=SellConfig(**_section(execution_raw, "sell")),
            flip_buy=FlipBuyConfig(**_section(execution_raw, "flip_buy")),
        ),
        time_decay=TimeDecayConfig(**_section(raw, "time_decay")),
        keywords=KeywordsConfig(**_section(raw, "keywords")),
        safety=SafetyConfig(**_section(raw, "safety")),
        sources=SourcesConfig(**_section(raw, "sources")),
        data_dir=Path(str(raw.get("data_dir", "data/binary-rule-bot"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )
    validate_binary_config(config)
    return config


def validate_binary_config(config: BinaryBotConfig) -> None:
    if config.market.held_side not in HELD_SIDES:
        raise ValueError(f"market.held_side must be one of {sorted(HELD_SIDES)!r}, got {config.market.held_side!r}")
    if config.entry.side not in ENTRY_SIDES:
        raise ValueError(f"entry.side must be YES or NO, got {config.entry.side!r}")
    if not config.market.held_side and not config.entry.enabled:
        raise ValueError("market.held_side is empty and entry is disabled: nothing to protect or enter")
    if not config.market.resolution_rules.strip():
        raise ValueError("market.resolution_rules is required: the classifier judges articles against it")
    if not config.market.deadline_date.strip():
        raise ValueError("market.deadline_date is required")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
