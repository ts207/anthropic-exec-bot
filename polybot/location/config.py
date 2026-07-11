from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Reused as-is: classifier/sources/safety config shapes are provider-agnostic
# and OperatorGate duck-types on ClassifierConfig's and SafetyConfig's
# attributes, so sharing the dataclasses keeps that gate reusable without
# modification.
from polybot.core.config import ClassifierConfig, SafetyConfig, SourcesConfig  # noqa: F401


@dataclass(frozen=True)
class OutcomeMarket:
    """One leg of the grouped categorical market (one location's Yes/No pair)."""

    name: str  # e.g. "qatar", "pakistan" -- must match LocationSignal.confirmed_location values
    label: str  # display label, e.g. "Qatar"
    condition_id: str
    yes_token_id: str
    no_token_id: str
    # Only rotation targets get an automatic buy-YES leg when confirmed; other
    # tracked outcomes (informational only) get sell-only treatment.
    rotation_target: bool = False


@dataclass(frozen=True)
class EventConfig:
    slug: str
    question: str
    deadline_date: str  # ISO date the grouped market resolves by
    # Key into outcomes, e.g. "qatar" -- the INITIAL/default location held YES.
    # Empty string means the bot starts flat; that is only valid with
    # entry.enabled, and once the bot has entered/rotated/exited, the live
    # holding in HoldingsStore overrides this value.
    held_location: str = ""
    resolution_rules: str = ""  # full market resolution-criteria text, fed to the classifier as context
    analyst_context: str = ""  # user's own thesis/background reasoning, fed to the classifier as context
    # Opt-in pin, mirroring polybot.iran.market_verifier's pattern: left blank
    # until an operator runs inspect-location, reviews the live rule text, and
    # pins its digest here. Blank means "not yet reviewed" -- not "verified".
    expected_rule_text_sha256: str = ""


@dataclass(frozen=True)
class PositionConfig:
    source: str = "onchain"
    held_yes_shares: float = 0.0
    max_yes_shares_to_sell: float = 100000.0
    max_rotation_usd_to_buy: float = 1000.0


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


@dataclass(frozen=True)
class BuyRotationConfig:
    enabled: bool = True
    max_price: float = 0.95
    usd_budget: float = 500.0
    skip_if_above_cap: bool = True
    max_spread: float = 0.50


@dataclass(frozen=True)
class EntryConfig:
    """Automated flat-to-position entry.

    Disabled by default: the bot stays a pure protection bot unless entry is
    explicitly configured. Entry uses the same evidence bar as a rotation buy
    (trusted tier-one source, confirmed senior round at a configured venue),
    and the same operator gate / live ack applies before any real order.
    """

    enabled: bool = False
    # Outcome keys eligible for automatic entry; must be a subset of outcomes.
    targets: list[str] = field(default_factory=list)
    usd_budget: float = 100.0
    max_price: float = 0.90
    max_spread: float = 0.50
    # Deterministic confirmation valuation.  The classifier extracts facts;
    # code converts the strongest accepted confirmation into this probability.
    confirmed_probability: float = 0.97
    min_edge: float = 0.05
    slippage_buffer: float = 0.01
    resolution_risk_buffer: float = 0.02
    # Any positive live fill creates exposure and is recorded, but fills below
    # these thresholds enter PARTIALLY_ENTERED and require reconciliation rather
    # than being silently treated as a complete entry.
    min_fill_usd: float = 5.0
    min_fill_fraction: float = 0.25
    reconcile_min_shares: float = 0.01
    # Lifetime cap on entry executions for this position config.
    max_entries: int = 1


def _default_source_likelihoods() -> dict[str, float]:
    return {
        "official_government": 2.5,
        "mediator_government": 2.5,
        "wire": 2.0,
        "state_media": 1.35,
        "other": 1.15,
    }


def _default_evidence_likelihoods() -> dict[str, float]:
    return {
        "confirmed_started": 8.0,
        "confirmed_scheduled": 5.0,
        "reported_indirect": 2.0,
        "speculative": 1.25,
        "denied": 4.0,
    }


@dataclass(frozen=True)
class ForecastConfig:
    """Anticipatory probability research. Always paper-only in this release."""

    enabled: bool = False
    paper_only: bool = True
    prior_probabilities: dict[str, float] = field(default_factory=dict)
    source_likelihoods: dict[str, float] = field(default_factory=_default_source_likelihoods)
    evidence_likelihoods: dict[str, float] = field(default_factory=_default_evidence_likelihoods)
    min_paper_edge: float = 0.12
    max_paper_price: float = 0.70
    paper_order_usd: float = 10.0
    slippage_buffer: float = 0.02
    resolution_risk_buffer: float = 0.03
    exit_remaining_edge: float = 0.03
    max_processed_articles: int = 2000
    model_version: str = "location-forecast-v2"
    # Paper fills use live quote snapshots when the runner is dry-run.  Tests
    # may still inject a deterministic quote adapter.
    live_quotes_in_dry_run: bool = True
    quote_refresh_seconds: float = 2.0
    max_quote_age_seconds: float = 10.0
    max_spread: float = 0.20
    fee_rate: float = 0.0
    simulated_slippage: float = 0.005


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    sell: SellConfig = field(default_factory=SellConfig)
    buy_rotation: BuyRotationConfig = field(default_factory=BuyRotationConfig)


@dataclass(frozen=True)
class TimeDecayConfig:
    enabled: bool = False
    trim_after_date: str = ""
    exit_after_date: str = ""
    trim_fraction: float = 0.25
    min_trim_price: float = 0.0
    min_exit_price: float = 0.0


@dataclass(frozen=True)
class PriceAlertConfig:
    enabled: bool = False
    outcome: str = ""
    thresholds: list[float] = field(default_factory=list)
    # When execution is dry-run, use public live CLOB books for monitoring
    # alerts instead of the synthetic DryRunTradingAdapter quote.
    live_quotes_in_dry_run: bool = False


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    interval_hours: float = 24.0


@dataclass(frozen=True)
class MarketVerificationMonitorConfig:
    enabled: bool = False
    interval_minutes: float = 30.0


@dataclass(frozen=True)
class MonitoringConfig:
    price_alerts: PriceAlertConfig = field(default_factory=PriceAlertConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    market_verification: MarketVerificationMonitorConfig = field(default_factory=MarketVerificationMonitorConfig)


@dataclass(frozen=True)
class LocationBotConfig:
    event: EventConfig
    outcomes: list[OutcomeMarket] = field(default_factory=list)
    position: PositionConfig = field(default_factory=PositionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    time_decay: TimeDecayConfig = field(default_factory=TimeDecayConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    data_dir: Path = Path("data/location-protection-bot")
    logs_dir: Path = Path("logs")

    def outcome(self, name: str) -> OutcomeMarket | None:
        normalized = name.strip().lower().replace(" ", "_")
        for outcome in self.outcomes:
            if outcome.name == normalized:
                return outcome
        return None

    def held_outcome(self) -> OutcomeMarket:
        outcome = self.outcome(self.event.held_location)
        if outcome is None:
            raise ValueError(f"held_location {self.event.held_location!r} not found in outcomes")
        return outcome

    def rotation_targets(self, held_location: str | None = None) -> list[OutcomeMarket]:
        held = self.event.held_location if held_location is None else held_location
        return [o for o in self.outcomes if o.rotation_target and o.name != held]

    def entry_target_names(self) -> set[str]:
        return {name.strip().lower().replace(" ", "_") for name in self.entry.targets}

    def entry_targets(self) -> list[OutcomeMarket]:
        names = self.entry_target_names()
        return [o for o in self.outcomes if o.name in names]


def load_location_config(path: Path) -> LocationBotConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML object")
    outcomes_raw = raw.get("outcomes", [])
    if not isinstance(outcomes_raw, list):
        raise ValueError("outcomes must be a list")
    outcomes = [OutcomeMarket(**_normalize_outcome(item)) for item in outcomes_raw]
    execution_raw = _section(raw, "execution")
    monitoring_raw = _section(raw, "monitoring")
    config = LocationBotConfig(
        event=EventConfig(**_section(raw, "event")),
        outcomes=outcomes,
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        entry=EntryConfig(**_section(raw, "entry")),
        forecast=ForecastConfig(**_section(raw, "forecast")),
        execution=ExecutionConfig(
            dry_run=bool(execution_raw.get("dry_run", True)),
            sell=SellConfig(**_section(execution_raw, "sell")),
            buy_rotation=BuyRotationConfig(**_section(execution_raw, "buy_rotation")),
        ),
        time_decay=TimeDecayConfig(**_section(raw, "time_decay")),
        monitoring=MonitoringConfig(
            price_alerts=PriceAlertConfig(**_section(monitoring_raw, "price_alerts")),
            heartbeat=HeartbeatConfig(**_section(monitoring_raw, "heartbeat")),
            market_verification=MarketVerificationMonitorConfig(**_section(monitoring_raw, "market_verification")),
        ),
        safety=SafetyConfig(**_section(raw, "safety")),
        sources=SourcesConfig(**_section(raw, "sources")),
        data_dir=Path(str(raw.get("data_dir", "data/location-protection-bot"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )
    _validate_entry(config)
    _validate_forecast(config)
    return config


def _validate_entry(config: LocationBotConfig) -> None:
    outcome_names = {o.name for o in config.outcomes}
    unknown = sorted(config.entry_target_names() - outcome_names)
    if unknown:
        raise ValueError(f"entry.targets not found in outcomes: {', '.join(unknown)}")
    if not config.event.held_location and not config.entry.enabled:
        raise ValueError("event.held_location is empty and entry is disabled: nothing to protect or enter")
    if config.entry.enabled and not config.entry.targets:
        raise ValueError("entry.enabled requires at least one entry.targets outcome key")
    if config.entry.enabled and config.event.held_location and config.event.held_location in config.entry_target_names():
        raise ValueError("event.held_location must not be listed in entry.targets (it is already held)")
    if config.event.held_location and config.outcome(config.event.held_location) is None:
        raise ValueError(f"event.held_location {config.event.held_location!r} not found in outcomes")
    if config.entry.usd_budget <= 0:
        raise ValueError("entry.usd_budget must be positive")
    for name, value in {
        "entry.max_price": config.entry.max_price,
        "entry.confirmed_probability": config.entry.confirmed_probability,
        "entry.max_spread": config.entry.max_spread,
    }.items():
        if value <= 0 or value > 1:
            raise ValueError(f"{name} must be in (0, 1]")
    for name, value in {
        "entry.min_edge": config.entry.min_edge,
        "entry.slippage_buffer": config.entry.slippage_buffer,
        "entry.resolution_risk_buffer": config.entry.resolution_risk_buffer,
        "entry.min_fill_usd": config.entry.min_fill_usd,
        "entry.reconcile_min_shares": config.entry.reconcile_min_shares,
    }.items():
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
    if config.entry.min_fill_fraction < 0 or config.entry.min_fill_fraction > 1:
        raise ValueError("entry.min_fill_fraction must be in [0, 1]")
    if config.entry.max_entries < 1:
        raise ValueError("entry.max_entries must be at least 1")
    if config.execution.buy_rotation.max_spread <= 0 or config.execution.buy_rotation.max_spread > 1:
        raise ValueError("execution.buy_rotation.max_spread must be in (0, 1]")
    if config.entry.confirmed_probability <= (
        config.entry.slippage_buffer + config.entry.resolution_risk_buffer
    ):
        raise ValueError("entry confirmation probability must exceed execution/rule-risk buffers")


def _validate_forecast(config: LocationBotConfig) -> None:
    forecast = config.forecast
    if not forecast.paper_only:
        raise ValueError("forecast.paper_only must remain true; anticipatory live execution is not implemented")
    if not forecast.enabled:
        return
    outcome_names = {outcome.name for outcome in config.outcomes}
    prior_names = {str(name).strip().lower().replace(" ", "_") for name in forecast.prior_probabilities}
    if prior_names != outcome_names:
        missing = sorted(outcome_names - prior_names)
        extra = sorted(prior_names - outcome_names)
        raise ValueError(f"forecast priors must cover every outcome exactly; missing={missing}, extra={extra}")
    priors = [float(value) for value in forecast.prior_probabilities.values()]
    if any(value < 0 or value > 1 for value in priors) or sum(priors) <= 0:
        raise ValueError("forecast prior probabilities must be non-negative with positive total")
    if abs(sum(priors) - 1.0) > 1e-6:
        raise ValueError("forecast prior probabilities must sum to 1")
    for mapping_name, mapping in {
        "source_likelihoods": forecast.source_likelihoods,
        "evidence_likelihoods": forecast.evidence_likelihoods,
    }.items():
        if not mapping or any(float(value) <= 0 for value in mapping.values()):
            raise ValueError(f"forecast.{mapping_name} values must be positive")
    for name, value in {
        "min_paper_edge": forecast.min_paper_edge,
        "slippage_buffer": forecast.slippage_buffer,
        "resolution_risk_buffer": forecast.resolution_risk_buffer,
        "exit_remaining_edge": forecast.exit_remaining_edge,
        "max_spread": forecast.max_spread,
        "fee_rate": forecast.fee_rate,
        "simulated_slippage": forecast.simulated_slippage,
    }.items():
        if value < 0 or value > 1:
            raise ValueError(f"forecast.{name} must be in [0, 1]")
    if forecast.max_paper_price <= 0 or forecast.max_paper_price > 1:
        raise ValueError("forecast.max_paper_price must be in (0, 1]")
    if forecast.paper_order_usd <= 0:
        raise ValueError("forecast.paper_order_usd must be positive")
    if not forecast.model_version.strip():
        raise ValueError("forecast.model_version must not be empty")
    if forecast.quote_refresh_seconds < 0:
        raise ValueError("forecast.quote_refresh_seconds must be non-negative")
    if forecast.max_quote_age_seconds <= 0:
        raise ValueError("forecast.max_quote_age_seconds must be positive")
    if forecast.max_processed_articles < 1:
        raise ValueError("forecast.max_processed_articles must be positive")


def _normalize_outcome(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized["name"] = str(item["name"]).strip().lower().replace(" ", "_")
    return normalized


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
