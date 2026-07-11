from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import LocationBotConfig
from .types import LocationSignal

STRONG_EVIDENCE = {"confirmed_started", "confirmed_scheduled"}
# A confirmed "no qualifying round will happen" signal usually arrives as a
# denial/collapse report: there is no senior round, so qualifies_as_senior_round
# is False and evidence_strength is often "denied". It must therefore be
# evaluated BEFORE the senior-round gate, with its own evidence set.
NO_MEETING_EVIDENCE = STRONG_EVIDENCE | {"denied"}
TIER_ONE_SOURCES = {"wire", "mediator_government", "official_government"}

# Fields a second (or later) classifier pass must agree on before a trade
# action is allowed to fire. Mirrors polybot.iran.decision's AGREEMENT_FIELDS:
# only the decision-relevant facts, not level/quote (self-derived / verified
# separately) or the free-text location_country_name (wording jitters even
# when the underlying fact matches).
AGREEMENT_FIELDS = [
    "source_is_trusted",
    "qualifies_as_senior_round",
    "round_status",
    "confirmed_location",
    "evidence_strength",
    "source_tier",
    "technical_location",
    "future_expected_formal_location",
    "final_decision_announced",
    "forecast_target_location",
    "evidence_direction",
]

# Ambiguous language that must NOT appear in the supporting quote for the
# no-meeting fast path below -- these describe a round that's delayed, not
# collapsed, and the settlement rules don't care about delays.
_AMBIGUOUS_DELAY_TERMS = ("postpone", "paused", "pause", "delay", "on hold", "suspended pending")


@dataclass(frozen=True)
class LocationDecision:
    action: str  # NO_ACTION | ALERT_ONLY | ROTATE_YES | EXIT_YES_ONLY | TRIM_YES | ENTER_YES
    level: str
    reason: str
    target_outcome: str | None = None  # outcome name to buy into, only set for ROTATE_YES/ENTER_YES
    factors: LocationSignal | None = None


def final_decision(config: LocationBotConfig, factors: LocationSignal, held: str | None = None) -> LocationDecision:
    # `held` is the LIVE holding (from HoldingsStore) when the caller has one;
    # the config value is only the initial default before any entry/rotation.
    held = held if held is not None else config.event.held_location

    if not factors.source_is_trusted:
        return LocationDecision("ALERT_ONLY", factors.level, "source_not_trusted", factors=factors)

    location = factors.confirmed_location
    strong = factors.evidence_strength in STRONG_EVIDENCE
    tier_one = factors.source_tier in TIER_ONE_SOURCES

    if location == "no_meeting":
        # Checked before the senior-round gate: a collapse/denial report never
        # qualifies as a senior round, so the gate below would make this branch
        # unreachable (bug found via smoke-location-classifier on 2026-07-06).
        if tier_one and factors.evidence_strength in NO_MEETING_EVIDENCE:
            return LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=factors)
        return LocationDecision("ALERT_ONLY", factors.level, "no_meeting_reported_unconfirmed", factors=factors)

    if factors.round_status == "technical_only" or not factors.qualifies_as_senior_round:
        if factors.technical_location not in {"none", "", held} and factors.future_expected_formal_location == held:
            return LocationDecision(
                "NO_ACTION",
                factors.level,
                "technical_location_not_qualifying_held_future_expected",
                factors=factors,
            )
        if factors.technical_location not in {"none", ""}:
            return LocationDecision("NO_ACTION", factors.level, "technical_location_not_qualifying", factors=factors)
        return LocationDecision("NO_ACTION", factors.level, "technical_or_non_qualifying", factors=factors)

    if location in {"none", "unclear", ""}:
        return LocationDecision("NO_ACTION", factors.level, "no_location_signal", factors=factors)

    if location == held:
        # Reinforces the held thesis; nothing to do regardless of evidence
        # strength (a weak report in our favor isn't a reason to act).
        return LocationDecision("NO_ACTION", factors.level, "held_location_reinforced", factors=factors)

    if not strong or not tier_one:
        # Some other location (or no-meeting) is *reported* but not yet
        # confirmed by a trustworthy source at "scheduled" or better -- a
        # scheduled round can still shift venue, so this is alert-only, not
        # a trade trigger.
        return LocationDecision(
            "ALERT_ONLY",
            factors.level,
            f"location_signal_not_yet_confirmed:{location}",
            factors=factors,
        )

    target = config.outcome(location)
    if target is not None and target.rotation_target and target.name != held:
        return LocationDecision("ROTATE_YES", "4B", f"confirmed_location:{location}", target_outcome=target.name, factors=factors)

    # A real, confirmed, non-held location that isn't one of the actively
    # rotated targets (or "other_specific"/unmapped name): sell the losing
    # side, but don't guess at buying into a market we haven't wired up.
    return LocationDecision("EXIT_YES_ONLY", "4B", f"confirmed_non_held_location_not_rotated:{location}", factors=factors)


def entry_decision(config: LocationBotConfig, factors: LocationSignal) -> LocationDecision:
    """Decision table for a FLAT bot (no held YES leg).

    The only trade action this can produce is ENTER_YES on a configured entry
    target, at the same evidence bar as a rotation buy: trusted tier-one
    source, qualifying senior round, confirmed_scheduled or better. Everything
    weaker alerts; everything irrelevant no-ops. There is nothing to protect
    while flat, so no sell action ever fires from here.
    """
    if not factors.source_is_trusted:
        return LocationDecision("ALERT_ONLY", factors.level, "source_not_trusted", factors=factors)

    location = factors.confirmed_location
    strong = factors.evidence_strength in STRONG_EVIDENCE
    tier_one = factors.source_tier in TIER_ONE_SOURCES

    if location == "no_meeting":
        # Checked before the senior-round gate for the same reason as in
        # final_decision. Flat means a collapse costs us nothing, but the
        # "no meeting" leg itself can be a configured entry target: a
        # tier-one confirmed collapse is then a buyable YES on that leg.
        target = config.outcome("no_meeting")
        if (
            config.entry.enabled
            and target is not None
            and target.name in config.entry_target_names()
            and tier_one
            and factors.evidence_strength in NO_MEETING_EVIDENCE
        ):
            return LocationDecision("ENTER_YES", "4B", "confirmed_location:no_meeting", target_outcome=target.name, factors=factors)
        return LocationDecision("ALERT_ONLY", factors.level, "no_meeting_reported_while_flat", factors=factors)

    if factors.round_status == "technical_only" or not factors.qualifies_as_senior_round:
        return LocationDecision("NO_ACTION", factors.level, "technical_or_non_qualifying", factors=factors)

    if location in {"none", "unclear", ""}:
        return LocationDecision("NO_ACTION", factors.level, "no_location_signal", factors=factors)

    if not strong or not tier_one:
        return LocationDecision("ALERT_ONLY", factors.level, f"entry_signal_not_yet_confirmed:{location}", factors=factors)

    if not config.entry.enabled:
        return LocationDecision("ALERT_ONLY", factors.level, f"entry_disabled_confirmed_location:{location}", factors=factors)

    target = config.outcome(location)
    if target is None:
        # A real, confirmed venue we haven't wired up (or "other_specific"):
        # nothing to buy safely -- alert so the operator can act manually.
        return LocationDecision("ALERT_ONLY", factors.level, f"confirmed_location_not_configured:{location}", factors=factors)
    if target.name not in config.entry_target_names():
        return LocationDecision("ALERT_ONLY", factors.level, f"entry_target_not_allowed:{location}", factors=factors)
    if not factors.final_decision_announced:
        # Opening a NEW position is held to a stricter bar than defending an
        # existing one: a confirmed-but-not-final venue can still shift.
        return LocationDecision("ALERT_ONLY", factors.level, f"entry_venue_not_final:{location}", factors=factors)
    return LocationDecision("ENTER_YES", "4B", f"confirmed_location:{location}", target_outcome=target.name, factors=factors)


def _is_unambiguous_collapse(factors: LocationSignal) -> bool:
    """Fast-path check for a genuine, tier-one-sourced no-meeting collapse.

    Deliberately stricter than the normal no_meeting branch in final_decision:
    requires a tier-one source AND excludes any hedge/delay language in the
    supporting quote (a "postponed" or "paused" round can still happen later
    at a different venue -- that is not the same as a confirmed collapse).
    """
    if factors.confirmed_location != "no_meeting":
        return False
    if factors.source_tier not in TIER_ONE_SOURCES:
        return False
    if factors.evidence_strength not in NO_MEETING_EVIDENCE:
        return False
    quote = (factors.quote_supporting_trigger or "").lower()
    return not any(term in quote for term in _AMBIGUOUS_DELAY_TERMS)


def classify_agreement(
    config: LocationBotConfig,
    passes: list[LocationSignal],
    *,
    held: str | None = None,
    flat: bool = False,
) -> LocationDecision:
    """Require multi-pass classifier agreement before any live trade action.

    A single classifier call is noisy on exactly the wording this market is
    settled on (technical vs. senior-level); requiring N passes to agree on
    the decision-relevant fields before acting on ROTATE_YES/EXIT_YES_ONLY/
    TRIM_YES/ENTER_YES catches a stray misread instead of trading on it.

    Exception: a genuine no-meeting collapse (see _is_unambiguous_collapse)
    is allowed to fast-path on the very first pass alone -- waiting for a
    second pass to agree on a confirmed collapse only delays protecting the
    position against a real, already-confirmed loss scenario. The fast path
    never applies while flat: with nothing held there is no loss to race,
    and an entry (buying the no-meeting leg) must meet the full agreement bar
    like any other new position.
    """

    def decide(signal: LocationSignal) -> LocationDecision:
        if flat:
            return entry_decision(config, signal)
        return final_decision(config, signal, held=held)

    if not passes:
        return LocationDecision("ALERT_ONLY", "3", "classifier_unavailable")
    first = passes[0]
    if _is_unambiguous_collapse(first) and not flat:
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
        return LocationDecision("ALERT_ONLY", "3", f"classifier_pass_disagreement:{','.join(differing)}", factors=first)
    return decide(first)


def time_decay_decision(config: LocationBotConfig, today: date | None = None) -> LocationDecision:
    if not config.time_decay.enabled:
        return LocationDecision("NO_ACTION", "0", "time_decay_disabled")
    current = today or date.today()
    if config.time_decay.exit_after_date and current >= date.fromisoformat(config.time_decay.exit_after_date):
        return LocationDecision("EXIT_YES_ONLY", "TIME", "time_decay_exit")
    if config.time_decay.trim_after_date and current >= date.fromisoformat(config.time_decay.trim_after_date):
        return LocationDecision("TRIM_YES", "TIME", "time_decay_trim")
    return LocationDecision("NO_ACTION", "0", "time_decay_not_reached")
