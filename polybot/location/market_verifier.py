from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from polybot.gamma import MarketMeta, fetch_event_by_slug, market_from_gamma

from .config import LocationBotConfig


@dataclass(frozen=True)
class OutcomeVerification:
    name: str
    label: str
    found: bool
    market_slug: str = ""
    question: str = ""
    condition_id_matches: bool = False
    yes_token_matches: bool = False
    no_token_matches: bool = False
    tradeable: bool = False
    tick_size: str = "0.01"
    neg_risk: bool = False
    mismatch_reason: str = ""


@dataclass(frozen=True)
class LocationMarketVerification:
    event_slug: str
    event_title: str
    rule_text: str
    rule_text_sha256: str
    outcomes: dict[str, OutcomeVerification]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_slug": self.event_slug,
            "event_title": self.event_title,
            "rule_text_sha256": self.rule_text_sha256,
            "outcomes": {name: asdict(v) for name, v in self.outcomes.items()},
        }


def verify_location_event(config: LocationBotConfig) -> LocationMarketVerification:
    """Fetch the live grouped ("neg risk") event from Gamma and cross-check it
    against every outcome hand-pasted into the config.

    This protects against the catastrophic class of bug: the classifier and
    decision engine reason correctly, but the config's token_id/condition_id
    for an outcome is stale, mistyped, or belongs to a different market
    entirely -- silently trading the wrong contract. Matching is done by
    Gamma's `groupItemTitle` field (the leg label, e.g. "Qatar"), which is
    what actually distinguishes each leg of a neg-risk grouped market.
    """
    event = fetch_event_by_slug(config.event.slug)
    markets_raw = event.get("markets")
    if not isinstance(markets_raw, list):
        raise ValueError(f"event {config.event.slug!r} has no markets list")

    live_by_label: dict[str, MarketMeta] = {}
    for raw in markets_raw:
        if not isinstance(raw, dict):
            continue
        try:
            meta = market_from_gamma(event, raw)
        except ValueError:
            continue
        label = str(raw.get("groupItemTitle") or "").strip().lower()
        if label:
            live_by_label[label] = meta

    rule_text = str(event.get("description") or "").strip()
    digest = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()
    if config.event.expected_rule_text_sha256 and config.event.expected_rule_text_sha256 != digest:
        raise ValueError(
            "location event rule text changed since expected_rule_text_sha256 was pinned; "
            "re-review with inspect-location before trading"
        )

    outcomes: dict[str, OutcomeVerification] = {}
    for outcome in config.outcomes:
        live = live_by_label.get(outcome.label.strip().lower())
        if live is None:
            outcomes[outcome.name] = OutcomeVerification(
                name=outcome.name,
                label=outcome.label,
                found=False,
                mismatch_reason="no live market found for this label on the event",
            )
            continue
        condition_id_matches = live.condition_id == outcome.condition_id
        yes_matches = live.yes_token_id == outcome.yes_token_id
        no_matches = live.no_token_id == outcome.no_token_id
        reason = ""
        if not condition_id_matches:
            reason = "condition_id_mismatch"
        elif not yes_matches or not no_matches:
            reason = "token_id_mismatch"
        outcomes[outcome.name] = OutcomeVerification(
            name=outcome.name,
            label=outcome.label,
            found=True,
            market_slug=live.market_slug,
            question=live.question,
            condition_id_matches=condition_id_matches,
            yes_token_matches=yes_matches,
            no_token_matches=no_matches,
            tradeable=live.tradeable(),
            tick_size=live.tick_size,
            neg_risk=live.neg_risk,
            mismatch_reason=reason,
        )

    return LocationMarketVerification(
        event_slug=str(event.get("slug") or config.event.slug),
        event_title=str(event.get("title") or ""),
        rule_text=rule_text,
        rule_text_sha256=digest,
        outcomes=outcomes,
    )


def verify_critical_outcomes(config: LocationBotConfig, verification: LocationMarketVerification) -> None:
    """Fail closed: raise if the held outcome, any active rotation target, or
    any configured entry target doesn't verify cleanly against the live Gamma
    event.

    Long-shot/untracked outcomes are deliberately not required to verify --
    if one of them is ever confirmed the bot only sells the held leg
    (EXIT_YES_ONLY), which doesn't require that outcome's token IDs at all.
    """
    critical = {o.name for o in config.rotation_targets()} | {o.name for o in config.entry_targets()}
    if config.event.held_location:
        critical.add(config.event.held_location)
    problems: list[str] = []
    for name in sorted(critical):
        v = verification.outcomes.get(name)
        if v is None or not v.found:
            problems.append(f"{name}: not found on live event")
        elif v.mismatch_reason:
            problems.append(f"{name}: {v.mismatch_reason}")
    if problems:
        raise ValueError("location market verification failed: " + "; ".join(problems))


def verify_all_outcomes(config: LocationBotConfig, verification: LocationMarketVerification, *, require_tradeable: bool = False) -> None:
    """Fail closed if any configured outcome no longer matches Gamma.

    The location bot carries hand-pasted token/condition IDs for every leg of
    the grouped market. Even when only a subset is an automatic rotation target,
    live arming should prove the whole 19-outcome map still describes the same
    live event the classifier is reasoning about.
    """
    problems: list[str] = []
    for outcome in config.outcomes:
        v = verification.outcomes.get(outcome.name)
        if v is None or not v.found:
            problems.append(f"{outcome.name}: not found on live event")
            continue
        if v.mismatch_reason:
            problems.append(f"{outcome.name}: {v.mismatch_reason}")
        if require_tradeable and not v.tradeable:
            problems.append(f"{outcome.name}: market_not_tradeable")
    if problems:
        raise ValueError("location market verification failed: " + "; ".join(problems))
