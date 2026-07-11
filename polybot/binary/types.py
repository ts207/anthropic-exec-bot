from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Re-exported so callers only need `polybot.binary.types` for both article and
# signal shapes (mirrors polybot.location.types).
from polybot.core.types import Article  # noqa: F401

Level = Literal["0", "1", "2", "3", "4A", "4B"]

# How strongly the article evidences its claim about the rule-qualifying
# event. Only "confirmed_started" or "confirmed_scheduled" are trusted enough
# to justify a real trade; weaker tiers are alert-only.
EvidenceStrength = Literal["confirmed_started", "confirmed_scheduled", "reported_indirect", "speculative", "denied"]

# Status of the market's rule-qualifying event as described by the article.
EventStatus = Literal["occurred", "underway", "scheduled", "expected", "rumored", "denied", "cancelled", "none", "unclear"]


@dataclass(frozen=True)
class RuleSignal:
    """Market-agnostic classification of one article against the market's
    verbatim resolution rules.

    Unlike the iran bot's SignalFactors (hardwired to one market's criteria),
    every judgement here is relative to whatever resolution rules the config
    supplies: "qualifying event" always means "an event that satisfies the YES
    resolution criteria of THIS market".
    """

    source_is_trusted: bool
    source_tier: str
    # The article's central event satisfies (or, if it happens as reported,
    # would satisfy) the market's YES resolution criteria. Technical,
    # preparatory, partial, or otherwise non-qualifying variants must be False.
    qualifies_under_rules: bool
    event_status: EventStatus
    evidence_strength: EvidenceStrength
    # The qualifying event occurs/begins before the market deadline.
    before_deadline: bool
    # The article confirms the YES criteria can no longer be met by the
    # deadline (cancellation, foreclosure, "will not happen").
    resolves_no: bool
    level: Level
    quote_supporting_trigger: str
    final_decision_announced: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleSignal":
        return cls(
            source_is_trusted=bool(raw.get("source_is_trusted")),
            source_tier=str(raw.get("source_tier") or "other"),
            qualifies_under_rules=bool(raw.get("qualifies_under_rules")),
            event_status=_event_status(str(raw.get("event_status") or "unclear")),
            evidence_strength=_evidence_strength(str(raw.get("evidence_strength") or "speculative")),
            before_deadline=bool(raw.get("before_deadline")),
            resolves_no=bool(raw.get("resolves_no")),
            level=_level(str(raw.get("level") or "3")),
            quote_supporting_trigger=str(raw.get("quote_supporting_trigger") or ""),
            final_decision_announced=bool(raw.get("final_decision_announced", True)),
        )


def _level(value: str) -> Level:
    if value in {"0", "1", "2", "3", "4A", "4B"}:
        return value  # type: ignore[return-value]
    return "3"


def _event_status(value: str) -> EventStatus:
    if value in {"occurred", "underway", "scheduled", "expected", "rumored", "denied", "cancelled", "none", "unclear"}:
        return value  # type: ignore[return-value]
    return "unclear"


def _evidence_strength(value: str) -> EvidenceStrength:
    if value in {"confirmed_started", "confirmed_scheduled", "reported_indirect", "speculative", "denied"}:
        return value  # type: ignore[return-value]
    return "speculative"
