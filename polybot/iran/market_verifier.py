from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from polybot.gamma import MarketMeta, select_market

from .config import IranBotConfig


@dataclass(frozen=True)
class MarketVerification:
    event_slug: str
    market_question: str
    target_leg: str
    rule_text: str
    rule_text_sha256: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    outcomes: list[str]
    outcome_token_mapping: dict[str, str]

    def as_dict(self) -> dict:
        return asdict(self)


def load_and_verify_market(config: IranBotConfig) -> tuple[MarketMeta, MarketVerification]:
    market = select_market(config.market.slug, config.market.expected_question_contains)
    return market, verify_market(market, config)


def verify_market(market: MarketMeta, config: IranBotConfig) -> MarketVerification:
    outcomes = [outcome.strip().lower() for outcome in market.outcomes]
    if outcomes != ["yes", "no"]:
        raise ValueError(f"YES/NO outcome mapping is ambiguous: {market.outcomes!r}")
    if not market.yes_token_id or not market.no_token_id or market.yes_token_id == market.no_token_id:
        raise ValueError("clobTokenIds cannot be parsed into distinct YES/NO token IDs")
    if config.market.target_leg.lower() not in market.question.lower() and config.market.target_leg.lower() not in market.market_slug.lower():
        raise ValueError(f"market question is not the configured target leg {config.market.target_leg!r}: {market.question!r}")
    if config.position.expected_yes_token_id and config.position.expected_yes_token_id != market.yes_token_id:
        raise ValueError("configured expected_yes_token_id does not match Gamma")
    if config.position.expected_no_token_id and config.position.expected_no_token_id != market.no_token_id:
        raise ValueError("configured expected_no_token_id does not match Gamma")
    rule_text = "\n\n".join(part for part in [market.description, market.resolution_source] if part).strip()
    if not rule_text:
        raise ValueError("rule text is empty; refusing to run without market rules")
    digest = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()
    if config.market.expected_rule_text_sha256 and config.market.expected_rule_text_sha256 != digest:
        raise ValueError("rule text changed unexpectedly")
    return MarketVerification(
        event_slug=market.event_slug,
        market_question=market.question,
        target_leg=config.market.target_leg,
        rule_text=rule_text,
        rule_text_sha256=digest,
        condition_id=market.condition_id,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        outcomes=market.outcomes,
        outcome_token_mapping={"YES": market.yes_token_id, "NO": market.no_token_id},
    )

