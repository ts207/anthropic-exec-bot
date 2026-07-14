from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .allocator import AllocationRequest, PortfolioAllocator
from .config import OpportunityConfig
from .types import MarketContext, Opportunity, TRADEABLE_STATES, market_dir_slug


class QuoteProviderProtocol(Protocol):
    def yes_best_ask(self, yes_token_id: str) -> float | None:
        ...

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        ...


# (market_id, outcome_name) -> (probability, source_label) or None
ProbabilityLookup = Callable[[str, str], tuple[float, str] | None]


def config_probability_lookup(config: OpportunityConfig) -> ProbabilityLookup:
    def lookup(market_id: str, outcome: str) -> tuple[float, str] | None:
        # Keys starting with "_" are estimate metadata (_decay, _as_of), never
        # outcome probabilities.
        if outcome.startswith("_"):
            return None
        market = config.probability_estimates.get(market_id)
        if not isinstance(market, dict):
            return None
        value = market.get(outcome)
        if value is None:
            return None
        return float(value), "config_estimate"

    return lookup


def forecast_probability_lookup(config: OpportunityConfig) -> ProbabilityLookup:
    """Read the paper forecast engine's persisted probabilities for a market.

    Emitted executor configs keep their state under
    <forecast_data_root>/<market-slug>[/dry_run]/forecast_probability.json.
    Stale or malformed state is ignored -- a probability the engine stopped
    updating must not keep pricing opportunities.
    """

    def lookup(market_id: str, outcome: str) -> tuple[float, str] | None:
        base = Path(config.forecast_data_root) / market_dir_slug(market_id)
        for candidate in (base / "dry_run" / "forecast_probability.json", base / "forecast_probability.json"):
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            if _too_old(str(raw.get("updated_at") or ""), config.forecast_max_age_hours):
                continue
            probabilities = raw.get("probabilities")
            if not isinstance(probabilities, dict) or outcome not in probabilities:
                continue
            try:
                return float(probabilities[outcome]), "forecast_state"
            except (TypeError, ValueError):
                continue
        return None

    return lookup


def combined_probability_lookup(config: OpportunityConfig) -> ProbabilityLookup:
    """Fresh forecast state wins over operator config estimates."""
    forecast = forecast_probability_lookup(config)
    static = config_probability_lookup(config)

    def lookup(market_id: str, outcome: str) -> tuple[float, str] | None:
        return forecast(market_id, outcome) or static(market_id, outcome)

    return lookup


def _remaining_fraction(as_of: str, deadline_iso: str) -> float | None:
    """Uniform-arrival deadline decay: P(event by deadline) estimated at
    `as_of` shrinks with the remaining-time fraction. A static estimate
    written three weeks ago is guaranteed miscalibrated today; this keeps it
    honest without any model. Returns None (no decay) when dates don't parse."""
    start = _parse_stamp(as_of)
    deadline = _parse_stamp(deadline_iso)
    if start is None or deadline is None or deadline <= start:
        return None
    now = datetime.now(timezone.utc)
    total = (deadline - start).total_seconds()
    remaining = (deadline - now).total_seconds()
    return max(0.0, min(1.0, remaining / total))


def _parse_stamp(text: str) -> datetime | None:
    cleaned = (text or "").strip().replace("Z", "+00:00")
    if not cleaned:
        return None
    try:
        stamp = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _too_old(updated_at: str, max_age_hours: float) -> bool:
    if max_age_hours <= 0:
        return False
    text = updated_at.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() > max_age_hours * 3600.0


def tradable_edge(probability: float, executable_price: float, config: OpportunityConfig) -> float:
    """The question is never 'is this outcome likely?' -- it is whether the
    estimated probability beats the executable price by enough to survive
    costs, resolution risk, and model uncertainty."""
    return round(
        probability
        - executable_price
        - config.slippage_buffer
        - config.resolution_risk_buffer
        - config.model_uncertainty_buffer,
        4,
    )


def scan_opportunities(
    contexts: list[MarketContext],
    config: OpportunityConfig,
    quotes: QuoteProviderProtocol,
    allocator: PortfolioAllocator,
    probability_lookup: ProbabilityLookup | None = None,
    forecast_calibrated: bool = False,
) -> list[Opportunity]:
    """Evaluate every outcome of every PAPER/LIVE-eligible market against its
    executable price. Every non-opportunity is still returned with blockers so
    the funnel report can explain where edge died."""
    probability_lookup = probability_lookup or combined_probability_lookup(config)
    results: list[Opportunity] = []
    for context in contexts:
        if context.state not in TRADEABLE_STATES:
            continue
        for outcome in context.outcomes:
            if not outcome.accepting_orders or outcome.closed:
                continue
            estimate = probability_lookup(context.market_id, outcome.name)
            if estimate is None:
                results.append(
                    Opportunity(
                        market_id=context.market_id,
                        outcome=outcome.name,
                        side="YES",
                        estimated_probability=0.0,
                        probability_source="none",
                        executable_price=None,
                        spread=None,
                        tradable_edge=None,
                        blockers=["no_probability_estimate"],
                    )
                )
                continue
            probability, source = estimate
            ask = quotes.yes_best_ask(outcome.yes_token_id)
            bid = quotes.yes_best_bid(outcome.yes_token_id)
            blockers: list[str] = []
            spread = round(ask - bid, 4) if ask is not None and bid is not None else None
            if ask is None:
                blockers.append("quote_unavailable")
            else:
                if ask > config.max_entry_price:
                    blockers.append(f"price_above_cap:{ask}")
                if spread is None:
                    blockers.append("spread_unknown")
                elif spread > config.max_spread:
                    blockers.append(f"spread_above_limit:{spread}")

            # Deadline decay for operator estimates flagged as event-arrival
            # probabilities ({"yes": 0.6, "_decay": true, "_as_of": "..."}).
            if source == "config_estimate":
                meta = config.probability_estimates.get(context.market_id) or {}
                if meta.get("_decay") and context.deadline_iso:
                    factor = _remaining_fraction(str(meta.get("_as_of") or ""), context.deadline_iso)
                    if factor is not None:
                        probability = round(probability * factor, 4)
                        source = "config_estimate_decayed"

            # Forecast-engine probabilities can't move allocatable money until
            # the calibration report proves they beat the market's own Brier.
            if source == "forecast_state" and config.require_calibrated_forecast and not forecast_calibrated:
                blockers.append("forecast_probability_uncalibrated")

            # Market-anchored blending + disagreement-scaled uncertainty: the
            # mid is the benchmark estimator until calibration data says
            # otherwise, and a large standing disagreement with the crowd
            # raises the edge bar instead of exciting the allocator.
            mid = round((ask + bid) / 2, 4) if ask is not None and bid is not None else None
            pricing_probability = probability
            disagreement_penalty = 0.0
            if mid is not None:
                if 0.0 <= config.model_weight < 1.0:
                    pricing_probability = round(config.model_weight * probability + (1.0 - config.model_weight) * mid, 4)
                disagreement_penalty = round(abs(probability - mid) * max(0.0, config.disagreement_buffer_scale), 4)

            risk_extra = 0.0
            if context.rule_analysis is not None:
                # Per-market resolution risk widens the buffer beyond the flat
                # base: ambiguous wording must clear a higher bar.
                risk_extra = round(context.rule_analysis.resolution_risk * getattr(config, "resolution_risk_scale", 0.0), 4)
            edge = (
                round(tradable_edge(pricing_probability, ask, config) - risk_extra - disagreement_penalty, 4)
                if ask is not None
                else None
            )
            if edge is not None and edge < config.min_edge:
                blockers.append(f"edge_below_minimum:{edge}")

            allocation_usd = 0.0
            if not blockers:
                # Thin markets graded into the small-size live tier are sized
                # to what their book can absorb, not the full per-order cap.
                requested = allocator.config.per_order_usd
                recommended = context.scores.get("recommended_max_order_usd")
                if recommended:
                    requested = min(requested, float(recommended))
                from .registry import region_of

                region = region_of(context.rule_analysis.parties) if context.rule_analysis else "global"
                request = AllocationRequest(
                    market_id=context.market_id,
                    event_slug=context.event_slug,
                    correlation_group=context.correlation_group or "uncategorized",
                    deadline_iso=context.deadline_iso,
                    usd=requested,
                    region=region,
                )
                allocation_usd, allocation_blockers = allocator.preview(request)
                blockers.extend(allocation_blockers)

            results.append(
                Opportunity(
                    market_id=context.market_id,
                    outcome=outcome.name,
                    side="YES",
                    estimated_probability=probability,
                    probability_source=source,
                    executable_price=ask,
                    spread=spread,
                    tradable_edge=edge,
                    blockers=blockers,
                    allocation_usd=allocation_usd if not blockers else 0.0,
                    detail={
                        "state": context.state,
                        "correlation_group": context.correlation_group,
                        "market_mid": mid,
                        "blended_probability": pricing_probability,
                        "disagreement_penalty": disagreement_penalty,
                    },
                )
            )
    results.sort(key=lambda item: (item.tradable_edge is None, -(item.tradable_edge or 0.0)))
    return results
