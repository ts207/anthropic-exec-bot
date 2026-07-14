from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from polybot.core.config import ClassifierConfig  # noqa: F401
from polybot.core.portfolio import AllocatorConfig  # noqa: F401


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
    # Liquidity/volume floors DEFAULT OFF: thin markets are the niche where a
    # confirmation bot out-waits bigger players, so liquidity must size orders,
    # never disqualify markets. Set floors only to skip literal dust.
    min_liquidity: float = 0.0
    min_volume: float = 0.0
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
    # Liquidity gates DEFAULT OFF (0 = disabled): liquidity sizes orders via
    # the recommendation below, it never blocks eligibility. Thin markets are
    # where confirmation edge persists longest -- professionals do not compete
    # for $20 of edge. Set thresholds >0 only if you explicitly want floors.
    min_liquidity_live: float = 0.0
    min_liquidity_paper: float = 0.0
    max_spread_live: float = 0.10
    max_days_to_deadline_live: float = 180.0
    max_markets_per_correlation_group: int = 2
    # Book-aware order sizing: every graded market gets
    # recommended_max_order_usd = max(min_order, liquidity * fraction); the
    # opportunity scan, config emission, and fleet all size to it (capped by
    # the allocator per-order limit). Deep books hit the per-order cap; thin
    # books trade at what they can absorb, floored at a minimum viable order.
    small_live_enabled: bool = True
    small_live_liquidity_fraction: float = 0.02
    small_live_min_order_usd: float = 5.0
    # The offline fixture rule analyzer may never produce live-eligible
    # markets (tests opt in explicitly).
    allow_fixture_analysis_live: bool = False
    # Per-market resolution-risk scaling: effective buffer =
    # opportunity.resolution_risk_buffer + analyzer_resolution_risk * this.
    resolution_risk_scale: float = 0.05


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
    # Forecast paper state, when present and fresh, overrides these.
    probability_estimates: dict[str, dict[str, float]] = field(default_factory=dict)
    # Where emitted executor configs keep their data dirs; the paper forecast
    # engine persists forecast_probability.json under
    # <root>/<market-slug>[/dry_run]/ and the scan reads it from there.
    forecast_data_root: str = "data/geopolitics"
    forecast_max_age_hours: float = 24.0
    # Effective resolution-risk buffer = resolution_risk_buffer +
    # analyzer_resolution_risk * resolution_risk_scale (per market).
    resolution_risk_scale: float = 0.05
    # Market-anchored blending: the market mid is itself a calibrated
    # probability estimator, usually better than an operator guess. The scan
    # prices edges with model_weight*model + (1-model_weight)*mid, so a
    # standing disagreement with the market must be LARGE to trade. Raise the
    # weight only after the calibration report proves the model beats the
    # market's own Brier score. 1.0 disables anchoring.
    model_weight: float = 0.35
    # Extra uncertainty buffer = |model - mid| * this scale: a big standing
    # disagreement with the crowd is itself evidence the model may be wrong,
    # so the edge bar rises exactly where miscalibration hurts most.
    disagreement_buffer_scale: float = 0.25
    # Forecast-state probabilities may only price allocatable opportunities
    # after the calibration report marks them calibrated (Brier beats the
    # market mid over min_resolved_for_calibration resolved outcomes).
    # Ungated they still appear in the scan/funnel with a blocker.
    require_calibrated_forecast: bool = True
    min_resolved_for_calibration: int = 20
    # Overpriced markets are edge too: price the NO side of every outcome
    # (executable NO ask = 1 - YES bid) with the same buffers and anchoring.
    scan_no_side: bool = True
    # Neg-risk internal consistency: when a grouped event's YES bids sum above
    # 1 (short every leg) or YES asks sum below 1 (buy every leg), the market
    # is arguing with itself -- no forecast needed. Minimum net edge after
    # per-leg slippage to report the arb.
    min_group_arb_edge: float = 0.02


@dataclass(frozen=True)
class ScheduleConfig:
    """Pacing for the recurring run-discovery loop."""

    interval_minutes: float = 60.0


@dataclass(frozen=True)
class FleetConfig:
    """One supervisor process trading EVERY eligible geopolitical market.

    The fleet runs the discovery cycle, emits/refreshes an executor config per
    LIVE_CONFIRMATION_ELIGIBLE market, arms each market's operator gate with
    `position_mode`, and supervises one bot subprocess per market. Bots for
    markets that hold a position are never stopped (defense continues even
    after a market is demoted); bots for flat demoted/closed markets are
    stopped. The single master kill switch is the shared operator global mode
    file (`set-fleet-mode off`).
    """

    enabled: bool = False
    # Concurrent bot cap; <= 0 means UNCAPPED (cover every eligible market).
    # Screen-tier classification keeps per-bot cost low enough to run wide.
    max_bots: int = 0
    # Operator position mode written for every managed market: keep
    # "alert_only" for a monitoring soak; "live" arms autonomous trading.
    position_mode: str = "alert_only"
    # Automatically acknowledge generated config hashes when arming live.
    # Required for unattended trading across many markets -- the operator
    # reviews and arms THE FLEET once instead of each market. The generated
    # configs are deterministic renders of pipeline state the operator
    # configured, and the master kill switch still stops everything.
    auto_ack: bool = False
    generated_dir: str = "configs/geopolitics/generated"
    # A running bot whose heartbeat is older than this is considered hung and
    # gets terminated + restarted on the next cycle.
    heartbeat_stale_seconds: float = 300.0
    # Crash-loop guard: markets restarted more than this many times per hour
    # stop being respawned and raise a fleet alarm instead.
    max_restarts_per_hour: int = 3


@dataclass(frozen=True)
class DiscoveryConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)
    allocator: AllocatorConfig = field(default_factory=AllocatorConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
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
        schedule=ScheduleConfig(**_section(raw, "schedule")),
        fleet=FleetConfig(**_section(raw, "fleet")),
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
