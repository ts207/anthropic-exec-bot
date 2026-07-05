from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import IranBotConfig
from .types import SignalFactors
from .verifier import quote_in_article


AGREEMENT_FIELDS = [
    "protect_no_position",
    "would_resolve_yes_if_true",
    "level",
    "technical_or_implementation_only",
    "formal_senior_level_round",
    "before_deadline",
    "recommended_action",
    "event_type",
    "seniority",
    "timing_relative_to_deadline",
    "source_tier",
]


@dataclass(frozen=True)
class Decision:
    action: str
    level: str
    reason: str
    factors: SignalFactors | None = None


def agree(left: SignalFactors, right: SignalFactors) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in AGREEMENT_FIELDS)


def classify_agreement(passes: list[SignalFactors], held_side: str = "NO") -> Decision:
    if not passes:
        return Decision("ALERT_ONLY", "3", "classifier_unavailable")
    first = passes[0]
    if len(passes) == 1:
        return final_decision(first, held_side=held_side)
    if not all(agree(first, other) for other in passes[1:]):
        return Decision("ALERT_ONLY", "3", "classifier_pass_disagreement", first)
    return final_decision(first, held_side=held_side)


def verify_quote_or_alert(decision: Decision, article_text: str) -> Decision:
    if decision.factors is None:
        return decision
    if decision.level not in {"4A", "4B"}:
        return decision
    if quote_in_article(decision.factors.quote_supporting_trigger, article_text):
        return decision
    return Decision("ALERT_ONLY", "3", "quote_verification_failed", decision.factors)


def final_decision(factors: SignalFactors, held_side: str = "NO") -> Decision:
    if held_side.upper() == "YES":
        return final_yes_decision(factors)
    return final_no_decision(factors)


def final_no_decision(factors: SignalFactors) -> Decision:
    if factors.technical_or_implementation_only:
        return Decision("NO_ACTION", factors.level, "technical_or_implementation_only", factors)
    if not factors.source_is_trusted:
        return Decision("ALERT_ONLY", factors.level, "source_not_trusted", factors)
    if not factors.protect_no_position:
        return Decision("NO_ACTION", factors.level, "does_not_break_no_thesis", factors)
    if factors.level == "4A":
        return Decision("SELL_NO_CONDITIONAL_BUY_YES", "4A", "formal_round_scheduled", factors)
    if factors.level == "4B":
        return Decision("SELL_NO_BUY_YES", "4B", "formal_round_begun", factors)
    return Decision("ALERT_ONLY", factors.level, "below_execution_level", factors)


def final_yes_decision(factors: SignalFactors) -> Decision:
    if factors.technical_or_implementation_only:
        return Decision("NO_ACTION", factors.level, "technical_or_implementation_only", factors)
    if not factors.source_is_trusted:
        return Decision("ALERT_ONLY", factors.level, "source_not_trusted", factors)

    event_type = factors.event_type
    seniority = factors.seniority
    timing = factors.timing_relative_to_deadline
    tier = factors.source_tier
    tier_one = tier in {"wire", "mediator_government", "official_government"}

    if event_type in {"round_occurred", "round_held"} and seniority == "senior" and timing == "before":
        return Decision("NO_ACTION", factors.level, "qualifying_round_occurred_hold", factors)
    if event_type == "round_scheduled" and seniority == "senior" and timing == "before":
        return Decision("NO_ACTION", factors.level, "senior_round_scheduled_hold_not_resolved", factors)
    if event_type == "round_postponed" and timing == "after" and seniority in {"senior", "unclear"} and tier_one:
        return Decision("EXIT_YES_OPTIONAL_BUY_NO", "4B", "senior_round_postponed_past_deadline", factors)
    if event_type in {"talks_cancelled", "strikes_or_breakdown"} and tier_one:
        return Decision("EXIT_YES_OPTIONAL_BUY_NO", "4B", event_type, factors)
    if event_type in {"round_postponed", "talks_cancelled", "strikes_or_breakdown"}:
        return Decision("TRIM_YES", "3", "negative_signal_not_exit_safe", factors)
    if seniority == "unclear" and event_type in {"round_scheduled", "round_occurred", "round_held"}:
        return Decision("ALERT_ONLY", "3", "unclear_seniority_no_exit", factors)
    return Decision("NO_ACTION", factors.level, "does_not_break_yes_thesis", factors)


def time_decay_decision(config: IranBotConfig, today: date | None = None) -> Decision:
    if config.market.held_side.upper() != "YES" or not config.time_decay.enabled:
        return Decision("NO_ACTION", "0", "time_decay_disabled")
    current = today or date.today()
    if config.time_decay.exit_after_date and current >= date.fromisoformat(config.time_decay.exit_after_date):
        return Decision("EXIT_YES_ONLY", "TIME", "time_decay_exit")
    if config.time_decay.trim_after_date and current >= date.fromisoformat(config.time_decay.trim_after_date):
        return Decision("TRIM_YES", "TIME", "time_decay_trim")
    return Decision("NO_ACTION", "0", "time_decay_not_reached")
