"""Autonomous probability estimation: the missing brain of the valuation path.

For every tradeable-state market with a rule analysis, produce the system's
OWN P(YES) -- an LLM read of the resolution rules, the deadline, and base
rates, deliberately WITHOUT the market price in the prompt so the estimate
is an independent signal rather than an echo of the mid.

Estimates are persisted in exactly the format forecast_probability_lookup()
reads (<forecast_data_root>/<market_dir_slug>/forecast_probability.json,
source "forecast_state"), so the existing machinery does the rest:

  - scan_opportunities prices every estimate against the executable book
    each cycle and records it in scan history;
  - capture_resolutions scores the estimates as markets resolve;
  - require_calibrated_forecast keeps forecast estimates from sizing live
    money until the calibration report proves they beat the market's Brier
    over min_resolved_for_calibration outcomes.

Autonomy is earned through calibration, never assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from polybot.log import log_event

from .types import MarketContext, TRADEABLE_STATES, market_dir_slug

ESTIMATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "p_yes": {"type": "number"},
        "confidence": {"type": "number"},
        "base_rate_reasoning": {"type": "string"},
        "key_factors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["p_yes", "confidence", "base_rate_reasoning", "key_factors"],
    "additionalProperties": False,
}

_PROMPT_TEMPLATE = """You are a careful geopolitical forecaster producing a calibrated probability
for a prediction-market question. You are NOT given the market's price on
purpose: your value is an independent estimate, not agreement.

Question: {question}
Deadline: {deadline} ({days_remaining:.0f} days from today, {today})

Resolution rules (verbatim, trimmed):
{rule_text}

Machine rule analysis:
- counts as YES: {counts}
- does NOT count: {does_not_count}
- summary: {summary}

Forecasting discipline:
- Anchor on base rates: most discrete geopolitical events (meetings,
  strikes, treaties, resignations) do NOT occur within any given short
  window. The status quo is the favorite until specific scheduled evidence
  says otherwise.
- Respect the deadline: P(event by deadline) shrinks as time runs out.
- Only the resolution rules matter. An event that "basically happened" but
  fails the rules resolves NO.
- Output p_yes as your calibrated probability that this market resolves YES.
"""


@dataclass(frozen=True)
class EstimatorConfig:
    enabled: bool = True
    # Sonnet by default: estimates run daily across the whole eligible
    # universe; opus is reserved for one-time rule analyses and confirm
    # passes where a single call gates a trade.
    model: str = "claude-sonnet-4-6"
    refresh_hours: float = 24.0
    max_per_cycle: int = 25
    cli_timeout_seconds: int = 120
    max_rule_text_chars: int = 1800


def estimate_market(
    context: MarketContext,
    config: EstimatorConfig,
    *,
    runner: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """One estimation call. Returns the parsed estimate payload or None."""
    analysis = context.rule_analysis
    if analysis is None:
        return None
    now = now or datetime.now(timezone.utc)
    days_remaining = _days_remaining(context.deadline_iso, now)
    prompt = _PROMPT_TEMPLATE.format(
        question=context.question,
        deadline=context.deadline_iso,
        days_remaining=days_remaining if days_remaining is not None else -1,
        today=now.date().isoformat(),
        rule_text=context.rule_text[: config.max_rule_text_chars],
        counts="; ".join(analysis.counts[:6]) or "(none listed)",
        does_not_count="; ".join(analysis.does_not_count[:6]) or "(none listed)",
        summary=analysis.summary[:400],
    )
    if runner is not None:
        stdout = runner(prompt)
    else:
        from polybot.core.claude_cli import run_claude_cli

        stdout = run_claude_cli(
            prompt,
            model=config.model,
            output_schema=ESTIMATE_SCHEMA,
            timeout_seconds=config.cli_timeout_seconds,
        )
    from polybot.core.claude_cli import extract_claude_cli_result

    result_text, _usage = extract_claude_cli_result(stdout)
    payload = json.loads(result_text)
    p_yes = float(payload["p_yes"])
    if not 0.0 <= p_yes <= 1.0:
        raise ValueError(f"p_yes out of range: {p_yes}")
    return payload


def refresh_estimates(
    contexts: list[MarketContext],
    *,
    forecast_data_root: str,
    config: EstimatorConfig,
    runner: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Estimate every stale/missing tradeable binary market, capped per cycle.

    Grouped markets are skipped in v1: a categorical distribution needs a
    different schema and normalization, and binary markets are where the
    calibration loop can start scoring immediately.
    """
    now = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"estimated": [], "skipped_fresh": 0, "skipped_grouped": 0, "errors": []}
    if not config.enabled:
        summary["disabled"] = True
        return summary
    candidates = [
        c for c in contexts
        if c.state in TRADEABLE_STATES and c.rule_analysis is not None
    ]
    # Nearest deadlines first: those estimates become scoreable soonest,
    # which is what unlocks calibration (and eventually live sizing).
    candidates.sort(key=lambda c: c.deadline_iso or "9999")
    for context in candidates:
        if len(summary["estimated"]) >= config.max_per_cycle:
            summary["budget_exhausted"] = True
            break
        if context.kind == "grouped":
            summary["skipped_grouped"] += 1
            continue
        out_path = Path(forecast_data_root) / market_dir_slug(context.market_id) / "forecast_probability.json"
        if _is_fresh(out_path, config.refresh_hours, now):
            summary["skipped_fresh"] += 1
            continue
        outcome_name = context.outcomes[0].name if context.outcomes else "yes"
        try:
            payload = estimate_market(context, config, runner=runner, now=now)
        except Exception as exc:  # one bad estimate must not stop the sweep
            log_event("discovery_estimate_failed", market_id=context.market_id, error=str(exc)[:300])
            summary["errors"].append(context.market_id)
            continue
        if payload is None:
            continue
        record = {
            "updated_at": now.isoformat(),
            "probabilities": {outcome_name: round(float(payload["p_yes"]), 4)},
            "confidence": payload.get("confidence"),
            "base_rate_reasoning": str(payload.get("base_rate_reasoning", ""))[:1000],
            "key_factors": payload.get("key_factors", [])[:8],
            "model": config.model,
            "source": "discovery_estimator",
            "rule_text_sha256": context.rule_text_sha256,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        log_event(
            "discovery_estimate_recorded",
            market_id=context.market_id,
            p_yes=record["probabilities"][outcome_name],
            confidence=record.get("confidence"),
        )
        summary["estimated"].append(context.market_id)
    return summary


def _is_fresh(path: Path, refresh_hours: float, now: datetime) -> bool:
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(raw.get("updated_at")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds() < refresh_hours * 3600.0


def _days_remaining(deadline_iso: str, now: datetime) -> float | None:
    try:
        deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - now).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return None
