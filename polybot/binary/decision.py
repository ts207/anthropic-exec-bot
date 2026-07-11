from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import BinaryBotConfig
from .types import RuleSignal

STRONG_EVIDENCE = {"confirmed_started", "confirmed_scheduled"}
# A confirmed "the YES criteria can no longer be met" signal usually arrives
# as a denial/cancellation report, so it carries its own evidence set (same
# rationale as polybot.location.decision's NO_MEETING_EVIDENCE).
FORECLOSURE_EVIDENCE = STRONG_EVIDENCE | {"denied"}
TIER_ONE_SOURCES = {"wire", "mediator_government", "official_government"}

ACTIONABLE_EVENT_STATUSES = {"scheduled", "underway", "occurred"}

# Fields a second (or later) classifier pass must agree on before a trade
# action is allowed to fire: only the decision-relevant facts, not
# level/quote (self-derived / verified separately).
AGREEMENT_FIELDS = [
    "source_is_trusted",
    "source_tier",
    "qualifies_under_rules",
    "event_status",
    "evidence_strength",
    "before_deadline",
    "resolves_no",
    "final_decision_announced",
]

# Ambiguous language that must NOT appear in the supporting quote for the
# foreclosure fast path below -- these describe a delayed event, not a
# foreclosed one, and most deadline markets don't care about delays that
# still land before the deadline.
_AMBIGUOUS_DELAY_TERMS = ("postpone", "paused", "pause", "delay", "on hold", "suspended pending")


@dataclass(frozen=True)
class BinaryDecision:
    action: str  # NO_ACTION | ALERT_ONLY | ENTER_YES | ENTER_NO | TRIM_HELD | EXIT_HELD
    level: str
    reason: str
    factors: RuleSignal | None = None


def held_decision(config: BinaryBotConfig, factors: RuleSignal, held_side: str) -> BinaryDecision:
    if held_side.upper() == "YES":
        return _held_yes_decision(factors)
    return _held_no_decision(factors)


def _held_yes_decision(factors: RuleSignal) -> BinaryDecision:
    if not factors.source_is_trusted:
        return BinaryDecision("ALERT_ONLY", factors.level, "source_not_trusted", factors)

    tier_one = factors.source_tier in TIER_ONE_SOURCES

    if factors.resolves_no:
        if tier_one and factors.evidence_strength in FORECLOSURE_EVIDENCE:
            return BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed", factors)
        return BinaryDecision("ALERT_ONLY", factors.level, "yes_foreclosure_reported_unconfirmed", factors)

    if factors.qualifies_under_rules and factors.event_status in ACTIONABLE_EVENT_STATUSES and factors.before_deadline:
        # The qualifying event is on track: the YES thesis is reinforced,
        # nothing to do regardless of evidence strength.
        return BinaryDecision("NO_ACTION", factors.level, "held_yes_thesis_reinforced", factors)

    return BinaryDecision("NO_ACTION", factors.level, "does_not_break_yes_thesis", factors)


def _held_no_decision(factors: RuleSignal) -> BinaryDecision:
    if not factors.source_is_trusted:
        return BinaryDecision("ALERT_ONLY", factors.level, "source_not_trusted", factors)

    tier_one = factors.source_tier in TIER_ONE_SOURCES
    strong = factors.evidence_strength in STRONG_EVIDENCE

    if factors.resolves_no:
        return BinaryDecision("NO_ACTION", factors.level, "held_no_thesis_reinforced", factors)

    if not factors.qualifies_under_rules:
        return BinaryDecision("NO_ACTION", factors.level, "does_not_break_no_thesis", factors)

    if not factors.before_deadline:
        # A qualifying event after the deadline still resolves this market NO.
        return BinaryDecision("NO_ACTION", factors.level, "qualifying_event_after_deadline", factors)

    if factors.event_status in {"occurred", "underway"} and strong and tier_one:
        return BinaryDecision("EXIT_HELD", "4B", "qualifying_event_begun_confirmed", factors)
    if factors.event_status == "scheduled" and strong and tier_one:
        return BinaryDecision("EXIT_HELD", "4A", "qualifying_event_scheduled_confirmed", factors)
    if factors.event_status in ACTIONABLE_EVENT_STATUSES:
        return BinaryDecision("ALERT_ONLY", factors.level, f"qualifying_event_not_yet_confirmed:{factors.event_status}", factors)

    return BinaryDecision("NO_ACTION", factors.level, "does_not_break_no_thesis", factors)


def entry_decision(config: BinaryBotConfig, factors: RuleSignal) -> BinaryDecision:
    """Decision table for a FLAT bot.

    entry.side YES: the only trade is ENTER_YES on a trusted tier-one
    confirmation of the rule-qualifying event before the deadline.
    entry.side NO: the only trade is ENTER_NO on a trusted tier-one
    confirmation that the YES criteria are foreclosed. Everything weaker
    alerts; everything irrelevant no-ops.
    """
    if not factors.source_is_trusted:
        return BinaryDecision("ALERT_ONLY", factors.level, "source_not_trusted", factors)

    tier_one = factors.source_tier in TIER_ONE_SOURCES
    strong = factors.evidence_strength in STRONG_EVIDENCE
    side = config.entry.side

    if factors.resolves_no:
        if side == "NO" and config.entry.enabled and tier_one and factors.evidence_strength in FORECLOSURE_EVIDENCE:
            return BinaryDecision("ENTER_NO", "4B", "foreclosure_confirmed", factors)
        return BinaryDecision("ALERT_ONLY", factors.level, "foreclosure_reported_while_flat", factors)

    if not factors.qualifies_under_rules:
        return BinaryDecision("NO_ACTION", factors.level, "no_rule_qualifying_signal", factors)

    if side == "NO":
        # Qualifying-event news weakens a prospective NO entry; never buy NO
        # into a confirmed qualifying event.
        return BinaryDecision("ALERT_ONLY", factors.level, "qualifying_signal_while_awaiting_no_entry", factors)

    if factors.event_status not in ACTIONABLE_EVENT_STATUSES:
        return BinaryDecision("ALERT_ONLY", factors.level, f"entry_signal_not_actionable:{factors.event_status}", factors)
    if not strong or not tier_one:
        return BinaryDecision("ALERT_ONLY", factors.level, f"entry_signal_not_yet_confirmed:{factors.event_status}", factors)
    if not factors.before_deadline:
        return BinaryDecision("ALERT_ONLY", factors.level, "qualifying_event_not_before_deadline", factors)
    if not config.entry.enabled:
        return BinaryDecision("ALERT_ONLY", factors.level, "entry_disabled_qualifying_event_confirmed", factors)
    if not factors.final_decision_announced:
        # Opening a NEW position is held to a stricter bar than defending an
        # existing one: a confirmed-but-not-final event can still change.
        return BinaryDecision("ALERT_ONLY", factors.level, "entry_event_not_final", factors)
    return BinaryDecision("ENTER_YES", "4B", f"qualifying_event_confirmed:{factors.event_status}", factors)


def _is_unambiguous_foreclosure(factors: RuleSignal) -> bool:
    """Fast-path check for a genuine, tier-one-sourced foreclosure of the YES
    criteria. Mirrors polybot.location.decision._is_unambiguous_collapse:
    stricter than the normal branch (delay/hedge language in the quote
    disqualifies it), because a postponed event can still land before the
    deadline."""
    if not factors.resolves_no:
        return False
    if factors.source_tier not in TIER_ONE_SOURCES:
        return False
    if factors.evidence_strength not in FORECLOSURE_EVIDENCE:
        return False
    quote = (factors.quote_supporting_trigger or "").lower()
    return not any(term in quote for term in _AMBIGUOUS_DELAY_TERMS)


def classify_agreement(
    config: BinaryBotConfig,
    passes: list[RuleSignal],
    *,
    held_side: str | None,
) -> BinaryDecision:
    """Require multi-pass classifier agreement before any trade action.

    Exception: while holding YES, a genuine foreclosure (see
    _is_unambiguous_foreclosure) fast-paths on the first pass alone --
    waiting for agreement only delays protecting the position against a
    confirmed loss. The fast path never applies while flat or holding NO:
    there is no loss to race there, and entries/exits in those states must
    meet the full agreement bar.
    """

    def decide(signal: RuleSignal) -> BinaryDecision:
        if held_side is None:
            return entry_decision(config, signal)
        return held_decision(config, signal, held_side)

    if not passes:
        return BinaryDecision("ALERT_ONLY", "3", "classifier_unavailable")
    first = passes[0]
    if held_side is not None and held_side.upper() == "YES" and _is_unambiguous_foreclosure(first):
        return decide(first)
    if len(passes) == 1:
        return decide(first)
    differing = sorted(
        {
            field
            for other in passes[1:]
            for field in AGREEMENT_FIELDS
            if getattr(first, field) != getattr(other, field)
        }
    )
    if differing:
        return BinaryDecision("ALERT_ONLY", "3", f"classifier_pass_disagreement:{','.join(differing)}", first)
    return decide(first)


def time_decay_decision(config: BinaryBotConfig, held_side: str | None, today: date | None = None) -> BinaryDecision:
    # Time decay only makes sense for a held YES on an event that has not
    # happened yet; a held NO gains as the deadline approaches without the
    # event, and a flat bot has nothing to decay.
    if held_side is None or held_side.upper() != "YES" or not config.time_decay.enabled:
        return BinaryDecision("NO_ACTION", "0", "time_decay_disabled")
    current = today or date.today()
    if config.time_decay.exit_after_date and current >= date.fromisoformat(config.time_decay.exit_after_date):
        return BinaryDecision("EXIT_HELD", "TIME", "time_decay_exit")
    if config.time_decay.trim_after_date and current >= date.fromisoformat(config.time_decay.trim_after_date):
        return BinaryDecision("TRIM_HELD", "TIME", "time_decay_trim")
    return BinaryDecision("NO_ACTION", "0", "time_decay_not_reached")
