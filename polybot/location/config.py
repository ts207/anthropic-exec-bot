from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Reused as-is: classifier/sources/safety config shapes are provider-agnostic
# and OperatorGate (polybot.iran.operator) duck-types on ClassifierConfig's and
# SafetyConfig's attributes, so sharing the dataclasses keeps that gate reusable
# without modification.
from polybot.iran.config import ClassifierConfig, SafetyConfig, SourcesConfig  # noqa: F401


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
    held_location: str  # key into outcomes, e.g. "qatar" -- the location currently held YES
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

    def rotation_targets(self) -> list[OutcomeMarket]:
        return [o for o in self.outcomes if o.rotation_target and o.name != self.event.held_location]


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
    return LocationBotConfig(
        event=EventConfig(**_section(raw, "event")),
        outcomes=outcomes,
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
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
