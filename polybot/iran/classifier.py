from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Any, Protocol

import requests

from .config import ClassifierConfig, SourcesConfig
from .types import Article, SignalFactors


def _bool_field() -> dict[str, str]:
    return {"type": "boolean"}


_ANTHROPIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_is_trusted": _bool_field(),
        "event_status": {"type": "string", "enum": ["none", "technical_only", "warning", "scheduled", "begun", "held", "unclear"]},
        "before_deadline": _bool_field(),
        "scheduled_before_july30": _bool_field(),
        "begun_before_july31": _bool_field(),
        "formal_senior_level_round": _bool_field(),
        "senior_us_representative_involved": _bool_field(),
        "senior_iran_representative_involved": _bool_field(),
        "in_person_or_indirect_in_person": _bool_field(),
        "peace_talks_or_negotiations": _bool_field(),
        "technical_or_implementation_only": _bool_field(),
        "protect_no_position": _bool_field(),
        "would_resolve_yes_if_true": _bool_field(),
        "recommended_action": {
            "type": "string",
            "enum": ["no_action", "hold", "alert_only", "trim_yes", "sell_yes_only", "sell_yes_and_buy_no", "sell_no_only", "sell_no_and_buy_yes"],
        },
        "level": {"type": "string", "enum": ["0", "1", "2", "3", "4A", "4B"]},
        "quote_supporting_trigger": {"type": "string"},
        "event_type": {
            "type": "string",
            "enum": ["round_occurred", "round_scheduled", "round_postponed", "talks_cancelled", "technical_only", "strikes_or_breakdown", "noise"],
        },
        "seniority": {"type": "string", "enum": ["senior", "technical", "unclear"]},
        "timing_relative_to_deadline": {"type": "string", "enum": ["before", "after", "unstated"]},
        "source_tier": {"type": "string", "enum": ["wire", "mediator_government", "official_government", "state_media", "other"]},
    },
    "required": [
        "source_is_trusted",
        "event_status",
        "before_deadline",
        "scheduled_before_july30",
        "begun_before_july31",
        "formal_senior_level_round",
        "senior_us_representative_involved",
        "senior_iran_representative_involved",
        "in_person_or_indirect_in_person",
        "peace_talks_or_negotiations",
        "technical_or_implementation_only",
        "protect_no_position",
        "would_resolve_yes_if_true",
        "recommended_action",
        "level",
        "quote_supporting_trigger",
        "event_type",
        "seniority",
        "timing_relative_to_deadline",
        "source_tier",
    ],
    "additionalProperties": False,
}


class FactorClassifier(Protocol):
    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        ...


class RuleBasedFixtureClassifier:
    """Deterministic classifier for dry-runs and tests; production should use LLMClassifier."""

    def __init__(self, sources: SourcesConfig):
        self.sources = sources

    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        text = f"{article.title}\n{article.raw_text}"
        lowered = text.lower()
        trusted = _domain_allowed(article.domain, self.sources.auto_trade_domains)
        technical = any(term in lowered for term in ("technical talks", "mou implementation", "communication channel", "working group", "staff-level", "staff level", "lower-level", "lower level"))
        negated = any(term in lowered for term in ("no meeting", "not scheduled", "no plans", "will not meet", "not negotiating", "denies"))
        named_us = "witkoff" in lowered
        named_iran = "araghchi" in lowered
        round_language = any(term in lowered for term in ("new round", "next round", "round of talks", "round of negotiations"))
        meeting_language = any(term in lowered for term in ("to meet", "will meet", "set to meet", "hold talks", "resume talks", "meet in doha"))
        formal = (
            ("formal" in lowered or "senior-level" in lowered or "senior level" in lowered or round_language)
            and ("talks" in lowered or "negotiations" in lowered or meeting_language)
        )
        senior_us = named_us or (bool(re.search(r"\b(us|u\.s\.|united states|american)\b", lowered)) and ("senior" in lowered or "representative" in lowered or "delegation" in lowered or round_language))
        senior_iran = named_iran or ("iran" in lowered and ("senior" in lowered or "representative" in lowered or "delegation" in lowered or round_language))
        seniority = "senior" if named_us or named_iran or "senior" in lowered or "high-level" in lowered or "high level" in lowered or "chief negotiator" in lowered or "foreign minister" in lowered else "technical" if technical else "unclear"
        peace = "peace talks" in lowered or "negotiations" in lowered or "talks" in lowered or (meeting_language and ("iran" in lowered or named_iran) and ("u.s." in lowered or "us " in lowered or "united states" in lowered or named_us))
        deadline_day = _target_july_day(market_rule_text)
        before = _before_deadline(lowered, deadline_day)
        after = _after_deadline(lowered, deadline_day)
        scheduled = before and any(term in lowered for term in ("scheduled", "confirmed", "set for", "will hold", "will resume", "to meet", "will meet", "set to meet", "hold talks"))
        begun = before and any(term in lowered for term in ("begun", "began", "underway", "started", "have opened", "has opened"))
        postponed = "postponed" in lowered or "delayed until" in lowered or "after july 17" in lowered
        cancelled = "cancelled" in lowered or "canceled" in lowered or "called off" in lowered
        breakdown = "strikes" in lowered or "breakdown" in lowered or "suspended indefinitely" in lowered
        source_tier = "wire" if article.domain in {"reuters.com", "apnews.com", "afp.com"} else "mediator_government" if article.domain in {"mofa.gov.qa", "fm.gov.om", "mofa.gov.pk"} else "official_government" if article.domain in {"state.gov", "whitehouse.gov", "mfa.gov.ir", "irna.ir"} else "other"

        if technical:
            level = "1"
            status = "technical_only"
            protect = False
            action = "no_action"
            event_type = "technical_only"
        elif negated:
            level = "1"
            status = "noise"
            protect = False
            action = "no_action"
            event_type = "noise"
        elif postponed:
            level = "4B" if after else "3"
            status = "postponed"
            protect = False
            action = "sell_yes_and_buy_no" if after else "trim_yes"
            event_type = "round_postponed"
        elif cancelled:
            level = "4B"
            status = "cancelled"
            protect = False
            action = "sell_yes_and_buy_no"
            event_type = "talks_cancelled"
        elif breakdown:
            level = "4B"
            status = "breakdown"
            protect = False
            action = "sell_yes_and_buy_no"
            event_type = "strikes_or_breakdown"
        elif formal and senior_us and senior_iran and begun:
            level = "4B"
            status = "begun"
            protect = True
            action = "sell_no_and_buy_yes"
            event_type = "round_occurred"
        elif formal and senior_us and senior_iran and scheduled:
            level = "4A"
            status = "scheduled"
            protect = True
            action = "sell_no_and_buy_yes"
            event_type = "round_scheduled"
        elif formal or "expand" in lowered or "broader issues" in lowered:
            level = "3"
            status = "warning"
            protect = False
            action = "alert_only"
            event_type = "noise"
        else:
            level = "0"
            status = "none"
            protect = False
            action = "no_action"
            event_type = "noise"

        return SignalFactors(
            source_is_trusted=trusted,
            event_status=status,
            before_deadline=before,
            scheduled_before_july30=scheduled,
            begun_before_july31=begun,
            formal_senior_level_round=formal and senior_us and senior_iran and not technical,
            senior_us_representative_involved=senior_us,
            senior_iran_representative_involved=senior_iran,
            in_person_or_indirect_in_person=("mediator" in lowered or "doha" in lowered or "oman" in lowered or "qatar" in lowered or "indirect" in lowered or "direct" in lowered),
            peace_talks_or_negotiations=peace,
            technical_or_implementation_only=technical,
            protect_no_position=protect,
            would_resolve_yes_if_true=begun,
            recommended_action=action,  # type: ignore[arg-type]
            level=level,  # type: ignore[arg-type]
            quote_supporting_trigger=_supporting_quote(article.raw_text, formal) if protect else _supporting_quote(article.raw_text, False),
            event_type=event_type,
            seniority=seniority,
            timing_relative_to_deadline="after" if after else "before" if before else "unstated",
            source_tier=source_tier,
        )


class LLMClassifier:
    def __init__(self, config: ClassifierConfig, sources: SourcesConfig, anthropic_client: object | None = None):
        self.config = config
        self.sources = sources
        self._anthropic_client = anthropic_client
        self.last_usage: dict[str, Any] | None = None

    def classify(self, article: Article, market_rule_text: str) -> SignalFactors:
        self.last_usage = None
        provider = self.config.provider.lower()
        prompt = _prompt(article, market_rule_text, self.sources)
        if provider == "openai":
            raw = self._openai(prompt)
        elif provider == "anthropic":
            raw = self._anthropic(prompt)
        else:
            raise RuntimeError(f"unsupported classifier provider: {self.config.provider}")
        return SignalFactors.from_dict(_json_object(raw))

    def _openai(self, prompt: str) -> str:
        key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY or LLM_API_KEY is required")
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": self.config.model,
                "temperature": self.config.temperature,
                "input": prompt,
            },
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("output_text"), str):
            return data["output_text"]
        chunks: list[str] = []
        for item in data.get("output", []) if isinstance(data, dict) else []:
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        return "\n".join(chunks)

    def _anthropic(self, prompt: str) -> str:
        # Structured outputs guarantee the response is valid JSON matching the
        # SignalFactors schema; temperature is not sent (rejected on Opus 4.7+).
        # Both agreement passes send an identical prompt; caching lets the
        # second pass read the first pass's prefix at ~0.1x input price.
        response = self._anthropic_client_or_build().messages.create(
            model=self.config.model,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            output_config={"format": {"type": "json_schema", "schema": _ANTHROPIC_OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        self.last_usage = _usage_dict(getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            raise RuntimeError("anthropic classifier refused the request; no trade")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise RuntimeError(f"anthropic classifier returned no text (stop_reason={response.stop_reason})")
        return text

    def _anthropic_client_or_build(self) -> Any:
        if self._anthropic_client is None:
            import anthropic

            key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY or LLM_API_KEY is required")
            self._anthropic_client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
        return self._anthropic_client


def build_classifier(config: ClassifierConfig, sources: SourcesConfig) -> FactorClassifier:
    if config.provider == "rule_based":
        return RuleBasedFixtureClassifier(sources)
    return LLMClassifier(config, sources)


def run_classifier_passes(classifier: FactorClassifier, article: Article, market_rule_text: str, passes: int) -> list[SignalFactors]:
    return [classifier.classify(article, market_rule_text) for _ in range(max(1, passes))]


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    fields = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "server_tool_use",
        "service_tier",
    ):
        if hasattr(usage, key):
            fields[key] = getattr(usage, key)
    return fields or None


def _domain_allowed(domain: str, allowed: list[str]) -> bool:
    normalized = domain.lower().removeprefix("www.")
    return any(normalized == item or normalized.endswith("." + item) for item in allowed)


def _target_july_day(context: str) -> int:
    match = re.search(r"\bjuly\s+([1-3]?\d)\b", context.lower())
    if match:
        day = int(match.group(1))
        if 1 <= day <= 31:
            return day
    return 17


def _mentioned_july_days(lowered: str) -> list[int]:
    return [int(day) for day in re.findall(r"\bjuly\s+([1-3]?\d)\b", lowered) if 1 <= int(day) <= 31]


def _before_deadline(lowered: str, deadline_day: int) -> bool:
    if "august" in lowered:
        return False
    days = _mentioned_july_days(lowered)
    if days:
        return min(days) <= deadline_day
    if f"before july {deadline_day}" in lowered or "before the deadline" in lowered:
        return True
    return False


def _after_deadline(lowered: str, deadline_day: int) -> bool:
    if "august" in lowered or "after the deadline" in lowered or f"after july {deadline_day}" in lowered:
        return True
    days = _mentioned_july_days(lowered)
    if days:
        return min(days) > deadline_day
    return False


def _supporting_quote(text: str, prefer_formal: bool) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    for sentence in sentences:
        lowered = sentence.lower()
        if prefer_formal and ("formal" in lowered or "senior-level" in lowered or "senior level" in lowered):
            return sentence.strip()
    return sentences[0].strip() if sentences else ""


def _json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"classifier did not return JSON object: {raw[:200]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("classifier JSON must be an object")
    return parsed


def _prompt(article: Article, market_rule_text: str, sources: SourcesConfig) -> str:
    schema = asdict(
        SignalFactors(
            source_is_trusted=True,
            event_status="none | technical_only | warning | scheduled | begun | held | unclear",
            before_deadline=True,
            scheduled_before_july30=True,
            begun_before_july31=False,
            formal_senior_level_round=True,
            senior_us_representative_involved=True,
            senior_iran_representative_involved=True,
            in_person_or_indirect_in_person=True,
            peace_talks_or_negotiations=True,
            technical_or_implementation_only=False,
            protect_no_position=True,
            would_resolve_yes_if_true=False,
            recommended_action="no_action | alert_only | sell_no_only | sell_no_and_buy_yes",  # type: ignore[arg-type]
            level="0 | 1 | 2 | 3 | 4A | 4B",  # type: ignore[arg-type]
            quote_supporting_trigger="exact quote from article",
            event_type="round_occurred | round_scheduled | round_postponed | talks_cancelled | technical_only | strikes_or_breakdown | noise",
            seniority="senior | technical | unclear",
            timing_relative_to_deadline="before | after | unstated",
            source_tier="wire | mediator_government | official_government | state_media | other",
        )
    )
    return (
        "Classify this one Polymarket protection signal. Return only strict JSON matching this shape.\n"
        "Auto-trade trusted domains: " + ", ".join(sources.auto_trade_domains) + "\n"
        "Extract fields. Do not emit a pure verdict.\n"
        "Do not treat technical, MoU implementation, working-level, or vague mediator diplomacy as execution-safe.\n"
        "For YES protection, postponed-past-deadline, cancellation, or renewed-strike/breakdown signals matter; silence is handled separately by time decay.\n"
        "Scheduled senior rounds before the deadline are hold signals, not resolution-safe. Only underway/resumed/held/begun senior rounds reset occurrence risk.\n"
        f"Schema: {json.dumps(schema, sort_keys=True)}\n"
        f"Market rule text:\n{market_rule_text}\n"
        f"Article domain: {article.domain}\nTitle: {article.title}\nArticle text:\n{_bounded_article_text(article.raw_text)}\n"
    )


def _bounded_article_text(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[article text truncated]"
