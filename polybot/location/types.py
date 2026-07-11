from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Re-exported so callers only need `polybot.location.types` for both article and
# signal shapes.
from polybot.core.types import Article  # noqa: F401

Level = Literal["0", "1", "2", "3", "4A", "4B"]

# How strongly the article evidences its location/status claim. Only
# "confirmed_scheduled" or "confirmed_started" are trusted enough to justify a
# real trade; weaker tiers are alert-only regardless of which location is named.
EvidenceStrength = Literal["confirmed_started", "confirmed_scheduled", "reported_indirect", "speculative", "denied"]
EvidenceDirection = Literal["supportive", "contradictory", "neutral"]

RoundStatus = Literal["none", "rumor", "scheduled", "underway", "concluded", "technical_only", "unclear"]


@dataclass(frozen=True)
class LocationSignal:
    source_is_trusted: bool
    qualifies_as_senior_round: bool
    round_status: RoundStatus
    # Free-text country name as reported (e.g. "Oman", "Kazakhstan"), independent
    # of whether it's one of the actively-rotated locations.
    location_country_name: str
    # Normalized bucket for the QUALIFYING formal/senior-round venue only: one
    # of the configured outcome keys, "other_specific" for a different named
    # country, "no_meeting" if no qualifying round will occur by the deadline,
    # or "none" when no qualifying venue is confirmed. Technical/preparatory
    # venue hints belong in technical_location, not here.
    confirmed_location: str
    evidence_strength: EvidenceStrength
    would_resolve_held_location_yes: bool
    would_resolve_held_location_no: bool
    level: Level
    quote_supporting_trigger: str
    source_tier: str = "other"
    # Extra body-aware venue fields used to prevent headline-driven false
    # rotation. Example: a headline says "Islamabad frontrunner" but the body
    # says Islamabad is only for technical talks and the high-level round is
    # expected in Doha. In that case confirmed_location must remain "none",
    # technical_location="pakistan", future_expected_formal_location="qatar".
    headline_location: str = "none"
    technical_location: str = "none"
    future_expected_formal_location: str = "none"
    final_decision_announced: bool = True
    # Forecast-only claim semantics.  These are deliberately separate from
    # confirmed_location: a report can discuss Qatar while explicitly denying
    # that Qatar will host.  In that case forecast_target_location="qatar" and
    # evidence_direction="contradictory".  Live execution still uses the
    # stricter confirmed/future fields above.
    forecast_target_location: str = "none"
    evidence_direction: EvidenceDirection = "neutral"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LocationSignal":
        return cls(
            source_is_trusted=bool(raw.get("source_is_trusted")),
            qualifies_as_senior_round=bool(raw.get("qualifies_as_senior_round")),
            round_status=_round_status(str(raw.get("round_status") or "unclear")),
            location_country_name=str(raw.get("location_country_name") or ""),
            confirmed_location=_confirmed_location(str(raw.get("confirmed_location") or "none")),
            evidence_strength=_evidence_strength(str(raw.get("evidence_strength") or "speculative")),
            would_resolve_held_location_yes=bool(raw.get("would_resolve_held_location_yes")),
            would_resolve_held_location_no=bool(raw.get("would_resolve_held_location_no")),
            level=_level(str(raw.get("level") or "3")),
            quote_supporting_trigger=str(raw.get("quote_supporting_trigger") or ""),
            source_tier=str(raw.get("source_tier") or "other"),
            headline_location=_confirmed_location(str(raw.get("headline_location") or "none")),
            technical_location=_confirmed_location(str(raw.get("technical_location") or "none")),
            future_expected_formal_location=_confirmed_location(str(raw.get("future_expected_formal_location") or "none")),
            final_decision_announced=bool(raw.get("final_decision_announced", True)),
            forecast_target_location=_confirmed_location(str(raw.get("forecast_target_location") or "none")),
            evidence_direction=_evidence_direction(str(raw.get("evidence_direction") or "neutral")),
        )


def _level(value: str) -> Level:
    if value in {"0", "1", "2", "3", "4A", "4B"}:
        return value  # type: ignore[return-value]
    return "3"


def _round_status(value: str) -> RoundStatus:
    if value in {"none", "rumor", "scheduled", "underway", "concluded", "technical_only", "unclear"}:
        return value  # type: ignore[return-value]
    return "unclear"


def _evidence_strength(value: str) -> EvidenceStrength:
    if value in {"confirmed_started", "confirmed_scheduled", "reported_indirect", "speculative", "denied"}:
        return value  # type: ignore[return-value]
    return "speculative"


def _confirmed_location(value: str) -> str:
    return value.strip().lower().replace(" ", "_") if value else "none"


def _evidence_direction(value: str) -> EvidenceDirection:
    if value in {"supportive", "contradictory", "neutral"}:
        return value  # type: ignore[return-value]
    return "neutral"
