from __future__ import annotations

import re

from polybot.iran.types import Article

from .config import LocationBotConfig

_MEETING_TERMS = (
    "meeting",
    "meetings",
    "meet",
    "met",
    "talks",
    "negotiations",
    "negotiation",
    "round",
    "dialogue",
    "summit",
    # "convene/convened" is the exact qualifying verb in the resolution rules
    # ("formally convened senior-level round"); without it a headline like
    # "US and Iran to convene in Doha" (no talks/round/meeting word) would fail
    # the gate and never be seen.
    "convene",
    "convened",
    "convenes",
    "convening",
)
_LOCATION_CONTEXT_TERMS = (
    "venue",
    "location",
    "host",
    "hosted",
    "hosting",
)
_CITY_ALIASES = {
    "doha",
    "islamabad",
    "geneva",
    "burgenstock",
    "muscat",
}
# Named senior-level principals from the position's own thesis (see
# analyst_context in qatar-sept30-yes-protection.yaml): their attendance is
# the actual technical-vs-senior-round signal the market settles on, and an
# article can carry that signal ("Witkoff met Araqchi today") without naming
# a venue at all -- so these escalate the same way a location term does,
# not just as a location-context fallback.
_SENIOR_OFFICIAL_TERMS = {
    # US side
    "witkoff",
    "kushner",
    "vance",
    "rubio",
    "barrack",
    # Iran side
    "araqchi",
    "ghalibaf",
    "gharibabadi",
    "larijani",
    "ravanchi",
    "takht-ravanchi",
    # IAEA / nuclear-file mediator whose named involvement often signals a
    # formally convened process
    "grossi",
}
_COLLAPSE_TERMS = (
    "no meeting",
    "no qualifying round",
    "will not meet",
    "would not meet",
    "won't meet",
    "called off",
    "cancelled",
    "canceled",
    "collapse",
    "collapsed",
    "suspended indefinitely",
    "terminated the negotiation process",
    "ended the negotiation process",
)


def should_escalate_location_article(article: Article, config: LocationBotConfig) -> bool:
    """Cheap deterministic pre-filter before spending classifier budget.

    The gate is intentionally broad: technical/preparatory meeting language is
    allowed through so the classifier/decision layer can explicitly mark it as
    non-qualifying. Collapse/no-meeting terms bypass the location-name check.
    """
    text = _normalize(f"{article.title}\n{article.raw_text}")
    if not text:
        return False
    if _contains_any(text, _COLLAPSE_TERMS):
        return True
    if not _contains_any(text, _MEETING_TERMS):
        return False
    location_terms = _configured_location_terms(config) | set(_CITY_ALIASES)
    if _contains_any(text, location_terms):
        return True
    if _contains_any(text, _SENIOR_OFFICIAL_TERMS):
        return True
    return _contains_any(text, _LOCATION_CONTEXT_TERMS)


def _configured_location_terms(config: LocationBotConfig) -> set[str]:
    terms: set[str] = set()
    for outcome in config.outcomes:
        for value in (outcome.name, outcome.label):
            normalized = _normalize(value.replace("_", " "))
            if normalized and "no meeting" not in normalized:
                terms.add(normalized)
    return terms


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _contains_any(text: str, terms) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) for term in terms)
