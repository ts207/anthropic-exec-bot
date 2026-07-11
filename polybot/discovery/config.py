from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from polybot.core.config import ClassifierConfig  # noqa: F401


def _default_include_keywords() -> list[str]:
    return [
        # diplomacy / conflict / statecraft vocabulary that marks a market as
        # geopolitical; matched against question + description + tags.
        "ceasefire", "peace talks", "negotiation", "sanction", "sanctions",
        "treaty", "diplomatic", "diplomacy", "military", "invasion", "strike",
        "missile", "nuclear", "election", "president", "prime minister",
        "government", "parliament", "coup", "war", "truce", "hostage",
        "annex", "border", "summit", "nato", "united nations", "security council",
        "mediator", "ambassador", "minister", "regime",
    ]


def _default_exclude_keywords() -> list[str]:
    return [
        "bitcoin", "ethereum", "crypto", "nba", "nfl", "mlb", "premier league",
        "champions league", "oscars", "grammy", "box office", "album",
        "temperature", "rainfall", "stock", "s&p", "nasdaq", "fed rate",
        "airdrop", "token launch", "tiktok followers",
    ]


def _default_include_tags() -> list[str]:
    return ["geopolitics", "politics", "world", "middle east", "war", "elections", "foreign policy"]


@dataclass(frozen=True)
class UniverseConfig:
    """What to enumerate and which events count as geopolitical candidates."""

    max_events: int = 300
    page_size: int = 100
    min_liquidity: float = 500.0
    min_volume: float = 1000.0
    max_days_to_deadline: float = 365.0
    include_keywords: list[str] = field(default_factory=_default_include_keywords)
    exclude_keywords: list[str] = field(default_factory=_default_exclude_keywords)
    include_tags: list[str] = field(default_factory=_default_include_tags)


@dataclass(frozen=True)
class ScoringConfig:
    """Thresholds for the eligibility state machine."""

    min_rule_text_chars: int = 200
    min_clarity_live: float = 0.75
    min_clarity_paper: float = 0.55
    min_observability_live: float = 0.7
    min_observability_paper: float = 0.5
    min_automation_live: float = 0.7
    max_resolution_risk_live: float = 0.35
    min_liquidity_live: float = 5000.0
    min_liquidity_paper: float = 500.0
    max_spread_live: float = 0.10
    max_days_to_deadline_live: float = 180.0
    max_markets_per_correlation_group: int = 2


@dataclass(frozen=True)
class OpportunityConfig:
    """Edge accounting: estimated probability must clear the executable price
    plus every buffer by min_edge before an outcome is an opportunity."""

    min_edge: float = 0.05
    slippage_buffer: float = 0.01
    resolution_risk_buffer: float = 0.02
    model_uncertainty_buffer: float = 0.03
    max_entry_price: float = 0.90
    max_spread: float = 0.15
    # Operator-supplied probability estimates: market_id -> outcome -> p.
    # Forecast paper state, when present, overrides these.
    probability_estimates: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class AllocatorConfig:
    """Portfolio-level exposure caps across every discovered market."""

    per_order_usd: float = 50.0
    per_market_usd: float = 100.0
    per_event_usd: float = 150.0
    per_group_usd: float = 200.0  # correlation-group concentration
    daily_usd: float = 300.0
    total_usd: float = 1000.0
    max_open_positions: int = 5
    max_per_deadline_week_usd: float = 400.0


@dataclass(frozen=True)
class DiscoveryConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)
    allocator: AllocatorConfig = field(default_factory=AllocatorConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    data_dir: Path = Path("data/discovery")
    logs_dir: Path = Path("logs")


def load_discovery_config(path: Path) -> DiscoveryConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return DiscoveryConfig(
        universe=UniverseConfig(**_section(raw, "universe")),
        scoring=ScoringConfig(**_section(raw, "scoring")),
        opportunity=OpportunityConfig(**_section(raw, "opportunity")),
        allocator=AllocatorConfig(**_section(raw, "allocator")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        data_dir=Path(str(raw.get("data_dir", "data/discovery"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
