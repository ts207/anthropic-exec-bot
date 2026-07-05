from __future__ import annotations

from dataclasses import dataclass


DANGER_TERMS = [
    "formal round",
    "senior-level",
    "senior level",
    "peace talks",
    "main negotiations",
    "formal negotiations",
    "resume negotiations",
    "senior representatives",
    "senior delegations",
    "new round",
    "next round of negotiations",
    "new round of talks",
    "high-level negotiations",
    "indirect talks",
    "to meet in doha",
    "will meet in doha",
    "araghchi",
    "witkoff",
    "postponed",
    "delayed until",
    "cancelled",
    "canceled",
    "called off",
    "suspended indefinitely",
    "breakdown",
    "strikes",
]

TECHNICAL_ONLY_TERMS = [
    "technical talks",
    "mou implementation",
    "implementation talks",
    "communication channel",
    "frozen funds",
    "hormuz",
    "shipping",
    "ceasefire breach",
    "working group",
    "staff-level",
    "staff level",
    "lower-level",
    "lower level",
    "preparatory",
    "monitoring",
    "deconfliction",
]

NEGATION_TERMS = [
    "denies",
    "no plans",
    "not scheduled",
    "rules out",
    "will not meet",
    "no meeting",
    "no direct talks",
    "not negotiating",
]


@dataclass(frozen=True)
class KeywordGateResult:
    escalate: bool
    danger_terms: list[str]
    technical_terms: list[str]
    negation_terms: list[str]


def keyword_gate(text: str) -> KeywordGateResult:
    lowered = text.lower()
    danger = [term for term in DANGER_TERMS if term in lowered]
    technical = [term for term in TECHNICAL_ONLY_TERMS if term in lowered]
    negation = [term for term in NEGATION_TERMS if term in lowered]
    return KeywordGateResult(
        escalate=bool(danger) and not (technical and not danger),
        danger_terms=danger,
        technical_terms=technical,
        negation_terms=negation,
    )
