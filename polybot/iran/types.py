from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Level = Literal["0", "1", "2", "3", "4A", "4B"]
RecommendedAction = Literal[
    "no_action",
    "hold",
    "alert_only",
    "trim_yes",
    "sell_yes_only",
    "sell_yes_and_buy_no",
    "sell_no_only",
    "sell_no_and_buy_yes",
]


@dataclass(frozen=True)
class Article:
    url: str
    domain: str
    title: str
    published_at: str | None
    fetched_at: str
    raw_text: str
    hash: str
    source_kind: str = "article"


@dataclass(frozen=True)
class SignalFactors:
    source_is_trusted: bool
    event_status: str
    before_deadline: bool
    scheduled_before_july30: bool
    begun_before_july31: bool
    formal_senior_level_round: bool
    senior_us_representative_involved: bool
    senior_iran_representative_involved: bool
    in_person_or_indirect_in_person: bool
    peace_talks_or_negotiations: bool
    technical_or_implementation_only: bool
    protect_no_position: bool
    would_resolve_yes_if_true: bool
    recommended_action: RecommendedAction
    level: Level
    quote_supporting_trigger: str
    event_type: str = "noise"
    seniority: str = "unclear"
    timing_relative_to_deadline: str = "unstated"
    source_tier: str = "other"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SignalFactors":
        return cls(
            source_is_trusted=bool(raw.get("source_is_trusted")),
            event_status=str(raw.get("event_status") or "unclear"),
            before_deadline=bool(raw.get("before_deadline")),
            scheduled_before_july30=bool(raw.get("scheduled_before_july30")),
            begun_before_july31=bool(raw.get("begun_before_july31")),
            formal_senior_level_round=bool(raw.get("formal_senior_level_round")),
            senior_us_representative_involved=bool(raw.get("senior_us_representative_involved")),
            senior_iran_representative_involved=bool(raw.get("senior_iran_representative_involved")),
            in_person_or_indirect_in_person=bool(raw.get("in_person_or_indirect_in_person")),
            peace_talks_or_negotiations=bool(raw.get("peace_talks_or_negotiations")),
            technical_or_implementation_only=bool(raw.get("technical_or_implementation_only")),
            protect_no_position=bool(raw.get("protect_no_position")),
            would_resolve_yes_if_true=bool(raw.get("would_resolve_yes_if_true")),
            recommended_action=_recommended_action(str(raw.get("recommended_action") or "alert_only")),
            level=_level(str(raw.get("level") or "3")),
            quote_supporting_trigger=str(raw.get("quote_supporting_trigger") or ""),
            event_type=str(raw.get("event_type") or raw.get("event_status") or "noise"),
            seniority=str(raw.get("seniority") or "unclear"),
            timing_relative_to_deadline=str(raw.get("timing_relative_to_deadline") or "unstated"),
            source_tier=str(raw.get("source_tier") or "other"),
        )


def _level(value: str) -> Level:
    if value in {"0", "1", "2", "3", "4A", "4B"}:
        return value  # type: ignore[return-value]
    return "3"


def _recommended_action(value: str) -> RecommendedAction:
    if value in {
        "no_action",
        "hold",
        "alert_only",
        "trim_yes",
        "sell_yes_only",
        "sell_yes_and_buy_no",
        "sell_no_only",
        "sell_no_and_buy_yes",
    }:
        return value  # type: ignore[return-value]
    return "alert_only"
