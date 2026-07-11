from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import ScoringConfig
from .types import MarketContext


def correlation_group(context: MarketContext) -> str:
    """Concentration key: markets deciding on the same actors move together.
    Two markets about the same parties are one risk, not two."""
    analysis = context.rule_analysis
    if analysis and analysis.parties:
        return "|".join(sorted(analysis.parties))
    if analysis and analysis.locations:
        return "|".join(sorted(analysis.locations))
    return "uncategorized"


def grade_market(
    context: MarketContext,
    scoring: ScoringConfig,
    *,
    now: datetime | None = None,
    group_counts: dict[str, int] | None = None,
    spread: float | None = None,
) -> MarketContext:
    """Compute per-dimension scores, apply the hard safety rules, and assign
    the market state. Pure function: returns an updated copy of the context.

    `group_counts` is the number of OTHER tradeable markets in each
    correlation group (exclude the market being graded)."""
    now = now or datetime.now(timezone.utc)
    group_counts = group_counts or {}
    reasons: list[str] = []
    scores: dict[str, float] = {}
    analysis = context.rule_analysis
    group = correlation_group(context)

    days_left = _days_to_deadline(context.deadline_iso, now)
    scores["liquidity"] = context.liquidity
    scores["volume"] = context.volume
    if days_left is not None:
        scores["time_horizon_days"] = round(days_left, 2)
    if spread is not None:
        scores["spread"] = spread
    scores["correlation_group_count"] = float(group_counts.get(group, 0))

    # Hard terminal conditions first.
    if context.closed or not context.active or not context.accepting_orders:
        return _finalize(context, "CLOSED", ["market_closed_or_not_accepting_orders"], scores, group)
    if days_left is not None and days_left <= 0:
        return _finalize(context, "CLOSED", ["deadline_passed"], scores, group)

    if len(context.rule_text.strip()) < scoring.min_rule_text_chars:
        reasons.append("missing_or_short_resolution_rules")
        return _finalize(context, "RULES_REVIEW_REQUIRED", reasons, scores, group)
    if not any(o.yes_token_id and o.no_token_id and o.condition_id for o in context.outcomes):
        return _finalize(context, "RULES_REVIEW_REQUIRED", ["unverified_token_mapping"], scores, group)
    if analysis is None:
        return _finalize(context, "RULES_REVIEW_REQUIRED", ["rule_analysis_missing"], scores, group)

    scores.update(
        {
            "rule_clarity": analysis.rule_clarity,
            "evidence_observability": analysis.evidence_observability,
            "resolution_risk": analysis.resolution_risk,
            "automation_suitability": analysis.automation_suitability,
        }
    )

    if analysis.discretionary:
        return _finalize(context, "MONITOR_ONLY", ["discretionary_rules"], scores, group)
    if analysis.rule_clarity < scoring.min_clarity_paper:
        return _finalize(context, "MONITOR_ONLY", [f"rule_clarity_below_paper_threshold:{analysis.rule_clarity}"], scores, group)
    if analysis.evidence_observability < scoring.min_observability_paper:
        return _finalize(context, "MONITOR_ONLY", [f"evidence_observability_below_paper_threshold:{analysis.evidence_observability}"], scores, group)
    if context.liquidity < scoring.min_liquidity_paper:
        return _finalize(context, "MONITOR_ONLY", [f"liquidity_below_paper_threshold:{context.liquidity:g}"], scores, group)

    live_blockers: list[str] = []
    if analysis.rule_clarity < scoring.min_clarity_live:
        live_blockers.append(f"rule_clarity_below_live_threshold:{analysis.rule_clarity}")
    if analysis.evidence_observability < scoring.min_observability_live:
        live_blockers.append(f"evidence_observability_below_live_threshold:{analysis.evidence_observability}")
    if analysis.automation_suitability < scoring.min_automation_live:
        live_blockers.append(f"automation_suitability_below_live_threshold:{analysis.automation_suitability}")
    if analysis.resolution_risk > scoring.max_resolution_risk_live:
        live_blockers.append(f"resolution_risk_above_live_threshold:{analysis.resolution_risk}")
    if context.liquidity < scoring.min_liquidity_live:
        live_blockers.append(f"liquidity_below_live_threshold:{context.liquidity:g}")
    if spread is not None and spread > scoring.max_spread_live:
        live_blockers.append(f"spread_above_live_threshold:{spread}")
    if days_left is not None and days_left > scoring.max_days_to_deadline_live:
        live_blockers.append(f"time_horizon_above_live_threshold:{days_left:.0f}d")
    if group_counts.get(group, 0) >= scoring.max_markets_per_correlation_group:
        live_blockers.append(f"correlation_group_limit:{group}")

    if live_blockers:
        return _finalize(context, "PAPER_ELIGIBLE", live_blockers, scores, group)
    return _finalize(context, "LIVE_CONFIRMATION_ELIGIBLE", ["all_live_gates_passed"], scores, group)


def _finalize(context: MarketContext, state: str, reasons: list[str], scores: dict[str, float], group: str) -> MarketContext:
    payload: dict[str, Any] = {
        **context.as_dict(),
        "state": state,
        "state_reasons": reasons,
        "scores": scores,
        "correlation_group": group,
    }
    return MarketContext.from_dict(payload)


def _days_to_deadline(deadline_iso: str, now: datetime) -> float | None:
    if not deadline_iso:
        return None
    text = deadline_iso.strip().replace("Z", "+00:00")
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (deadline - now).total_seconds() / 86400.0
