from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.storage import append_jsonl
from polybot.core.types import Article
from polybot.core.verifier import quote_in_article

from .config import LocationBotConfig
from .holdings import _atomic_json_write
from .quotes import QuoteAdapter
from .types import LocationSignal


FORECAST_STATE_VERSION = 2


@dataclass
class ProbabilityState:
    updated_at: str
    probabilities: dict[str, float]
    state_version: int = FORECAST_STATE_VERSION
    model_version: str = ""
    config_fingerprint: str = ""
    processed_articles: list[str] = field(default_factory=list)
    processed_claims: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PaperState:
    updated_at: str
    state_version: int = FORECAST_STATE_VERSION
    model_version: str = ""
    config_fingerprint: str = ""
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    realized_pnl: float = 0.0


class ForecastPaperEngine:
    """Evidence model and simulated execution with a read-only quote surface."""

    def __init__(self, config: LocationBotConfig, adapter: QuoteAdapter, data_dir: Path, logs_dir: Path):
        self.config = config
        self.adapter = adapter
        self.data_dir = data_dir
        self.logs_dir = logs_dir
        self.probability_path = data_dir / "forecast_probability.json"
        self.paper_path = data_dir / "forecast_paper.json"
        self.config_fingerprint = _config_fingerprint(config)

    def process(self, article: Article, passes: list[LocationSignal]) -> dict[str, Any]:
        if not self.config.forecast.enabled:
            return {"enabled": False}
        if not passes:
            return {"enabled": True, "updated": False, "reason": "no_classifier_passes"}
        if not _passes_agree(passes):
            return {"enabled": True, "updated": False, "reason": "classifier_pass_disagreement"}

        signal = passes[0]
        state = self._load_probability()
        if article.hash in state.processed_articles:
            return {"enabled": True, "updated": False, "reason": "article_already_processed"}
        if not quote_in_article(signal.quote_supporting_trigger, article.raw_text):
            self._mark_processed(state, article.hash, None)
            self._save_probability(state)
            return {"enabled": True, "updated": False, "reason": "quote_verification_failed"}

        target, target_kind = _evidence_target(self.config, signal)
        if target is None or signal.evidence_direction == "neutral":
            self._mark_processed(state, article.hash, None)
            self._save_probability(state)
            return {"enabled": True, "updated": False, "reason": "no_directional_forecast_target"}

        claim = _claim_fingerprint(target, signal.evidence_direction, signal.quote_supporting_trigger)
        if claim in state.processed_claims:
            self._mark_processed(state, article.hash, None)
            self._save_probability(state)
            return {"enabled": True, "updated": False, "reason": "claim_already_processed", "target": target}

        likelihood = _likelihood_ratio(self.config, signal, target_kind)
        before = dict(state.probabilities)
        state.probabilities = _bayes_target_update(state.probabilities, target, likelihood)
        now = _now()
        observation = {
            "article_hash": article.hash,
            "url": article.url,
            "domain": article.domain,
            "target": target,
            "target_kind": target_kind,
            "direction": signal.evidence_direction,
            "source_tier": signal.source_tier,
            "evidence_strength": signal.evidence_strength,
            "likelihood_ratio": likelihood,
            "before": before,
            "after": state.probabilities,
            "quote": signal.quote_supporting_trigger,
            "observed_at": now,
        }
        state.observations.append(observation)
        state.observations = state.observations[-self.config.forecast.max_processed_articles :]
        self._mark_processed(state, article.hash, claim)
        state.updated_at = now
        self._save_probability(state)

        paper = self._load_paper()
        exits = self._paper_exits(paper, state.probabilities, trigger_id=article.hash, trigger_kind="evidence")
        opened = None
        if signal.evidence_direction == "supportive":
            opened = self._paper_entry(paper, state.probabilities, target, signal, article)
        paper.updated_at = now
        self._save_paper(paper)
        report = {
            "enabled": True,
            "paper_only": True,
            "updated": True,
            "observation": observation,
            "opened": opened,
            "exits": exits,
            "positions": paper.positions,
            "realized_pnl": paper.realized_pnl,
        }
        append_jsonl(self.logs_dir / "location_forecast_paper.jsonl", report)
        return report

    def mark_cycle(self) -> dict[str, Any]:
        """Revalue and exit paper positions on every poll, without new news."""

        if not self.config.forecast.enabled:
            return {"enabled": False, "exits": []}
        probability = self._load_probability()
        paper = self._load_paper()
        trigger_id = f"mark:{_now()}"
        exits = self._paper_exits(paper, probability.probabilities, trigger_id=trigger_id, trigger_kind="market_mark")
        if exits:
            paper.updated_at = _now()
            self._save_paper(paper)
            append_jsonl(
                self.logs_dir / "location_forecast_paper.jsonl",
                {"enabled": True, "paper_only": True, "updated": True, "mark_cycle": True, "exits": exits},
            )
        return {"enabled": True, "paper_only": True, "updated": bool(exits), "exits": exits}

    def snapshot(self) -> dict[str, Any]:
        probability = self._load_probability()
        paper = self._load_paper()
        marks: dict[str, Any] = {}
        unrealized = 0.0
        for outcome_name, position in paper.positions.items():
            outcome = self.config.outcome(outcome_name)
            try:
                quote = self._quote(outcome.yes_token_id) if outcome is not None else {"valid": False}
                bid = quote.get("bid") if quote.get("valid") else None
                shares = float(position.get("shares") or 0.0)
                cost = float(position.get("cost_usd") or 0.0)
                gross = shares * float(bid) if bid is not None else None
                value = gross * (1.0 - self.config.forecast.fee_rate) if gross is not None else None
                if value is not None:
                    unrealized += value - cost
                marks[outcome_name] = {**quote, "value": value, "cost": cost}
            except Exception as exc:
                marks[outcome_name] = {"valid": False, "error": str(exc)}
        return {
            "enabled": self.config.forecast.enabled,
            "paper_only": True,
            "model_version": self.config.forecast.model_version,
            "config_fingerprint": self.config_fingerprint,
            "probabilities": probability.probabilities,
            "positions": paper.positions,
            "marks": marks,
            "realized_pnl": paper.realized_pnl,
            "unrealized_pnl": unrealized,
            "observation_count": len(probability.observations),
            "trade_count": len(paper.trades),
        }

    def _paper_entry(
        self,
        paper: PaperState,
        probabilities: dict[str, float],
        target: str,
        signal: LocationSignal,
        article: Article,
    ) -> dict[str, Any] | None:
        if target in paper.positions:
            return None
        if (
            signal.final_decision_announced
            and signal.qualifies_as_senior_round
            and signal.evidence_strength in {"confirmed_started", "confirmed_scheduled"}
        ):
            return None
        outcome = self.config.outcome(target)
        if outcome is None:
            return None
        quote = self._quote(outcome.yes_token_id)
        ask = quote.get("ask")
        if not quote.get("valid") or ask is None or float(ask) <= 0:
            return None
        fill_price = min(1.0, float(ask) + self.config.forecast.simulated_slippage)
        if fill_price > self.config.forecast.max_paper_price:
            return None
        fair = probabilities[target]
        edge = (
            fair
            - fill_price
            - self.config.forecast.slippage_buffer
            - self.config.forecast.resolution_risk_buffer
            - self.config.forecast.fee_rate
        )
        if edge < self.config.forecast.min_paper_edge:
            return None
        usd = self.config.forecast.paper_order_usd
        fee = usd * self.config.forecast.fee_rate
        shares = max(0.0, usd - fee) / fill_price
        trade = {
            "trade_id": hashlib.sha256(f"open:{article.hash}:{target}".encode()).hexdigest()[:20],
            "side": "BUY_YES",
            "outcome": target,
            "price": fill_price,
            "quoted_ask": ask,
            "shares": shares,
            "usd": usd,
            "fee_usd": fee,
            "fair_probability": fair,
            "edge_after_buffers": edge,
            "quote": quote,
            "article_hash": article.hash,
            "timestamp": _now(),
        }
        paper.positions[target] = {
            "outcome": target,
            "shares": shares,
            "entry_price": fill_price,
            "entry_probability": fair,
            "cost_usd": usd,
            "opened_at": trade["timestamp"],
            "article_hash": article.hash,
        }
        paper.trades.append(trade)
        return trade

    def _paper_exits(
        self,
        paper: PaperState,
        probabilities: dict[str, float],
        *,
        trigger_id: str,
        trigger_kind: str,
    ) -> list[dict[str, Any]]:
        exits: list[dict[str, Any]] = []
        for outcome_name, position in list(paper.positions.items()):
            outcome = self.config.outcome(outcome_name)
            if outcome is None:
                continue
            try:
                quote = self._quote(outcome.yes_token_id)
            except Exception:
                continue
            bid = quote.get("bid")
            if not quote.get("valid") or bid is None or float(bid) <= 0:
                continue
            fair = probabilities.get(outcome_name, 0.0)
            remaining_edge = fair - float(bid) - self.config.forecast.resolution_risk_buffer
            if remaining_edge > self.config.forecast.exit_remaining_edge:
                continue
            shares = float(position.get("shares") or 0.0)
            cost = float(position.get("cost_usd") or 0.0)
            fill_price = max(0.0, float(bid) - self.config.forecast.simulated_slippage)
            gross = shares * fill_price
            fee = gross * self.config.forecast.fee_rate
            proceeds = gross - fee
            trade = {
                "trade_id": hashlib.sha256(f"close:{trigger_id}:{outcome_name}".encode()).hexdigest()[:20],
                "side": "SELL_YES",
                "outcome": outcome_name,
                "price": fill_price,
                "quoted_bid": bid,
                "shares": shares,
                "gross_proceeds_usd": gross,
                "fee_usd": fee,
                "proceeds_usd": proceeds,
                "cost_usd": cost,
                "pnl": proceeds - cost,
                "fair_probability": fair,
                "remaining_edge": remaining_edge,
                "quote": quote,
                "trigger_id": trigger_id,
                "trigger_kind": trigger_kind,
                "timestamp": _now(),
            }
            paper.realized_pnl += proceeds - cost
            paper.trades.append(trade)
            del paper.positions[outcome_name]
            exits.append(trade)
        return exits

    def _quote(self, token_id: str) -> dict[str, Any]:
        snapshot_method = getattr(self.adapter, "quote_snapshot", None)
        if callable(snapshot_method):
            raw = snapshot_method(token_id)
            ask = _as_float(raw.get("best_ask")) if isinstance(raw, dict) else None
            bid = _as_float(raw.get("best_bid")) if isinstance(raw, dict) else None
            staleness = _as_float(raw.get("staleness")) if isinstance(raw, dict) else None
            source = str(raw.get("source") or self.adapter.__class__.__name__) if isinstance(raw, dict) else self.adapter.__class__.__name__
        else:
            ask = self.adapter.yes_best_ask(token_id)
            bid = self.adapter.yes_best_bid(token_id)
            staleness = 0.0
            source = self.adapter.__class__.__name__
        spread = ask - bid if ask is not None and bid is not None else None
        valid = (
            (staleness is None or staleness <= self.config.forecast.max_quote_age_seconds)
            and (spread is None or spread <= self.config.forecast.max_spread)
        )
        return {
            "ask": ask,
            "bid": bid,
            "spread": spread,
            "staleness_seconds": staleness,
            "source": source,
            "valid": valid,
        }

    def _new_probability(self) -> ProbabilityState:
        return ProbabilityState(
            updated_at=_now(),
            probabilities=_normalized(self.config.forecast.prior_probabilities),
            model_version=self.config.forecast.model_version,
            config_fingerprint=self.config_fingerprint,
        )

    def _new_paper(self) -> PaperState:
        return PaperState(
            updated_at=_now(),
            model_version=self.config.forecast.model_version,
            config_fingerprint=self.config_fingerprint,
        )

    def _load_probability(self) -> ProbabilityState:
        if not self.probability_path.exists():
            return self._new_probability()
        raw = json.loads(self.probability_path.read_text(encoding="utf-8"))
        if not self._compatible(raw):
            self._archive_incompatible(self.probability_path)
            return self._new_probability()
        return ProbabilityState(
            updated_at=str(raw.get("updated_at") or ""),
            probabilities=_normalized(raw.get("probabilities") if isinstance(raw.get("probabilities"), dict) else {}),
            state_version=int(raw.get("state_version") or 0),
            model_version=str(raw.get("model_version") or ""),
            config_fingerprint=str(raw.get("config_fingerprint") or ""),
            processed_articles=[str(value) for value in raw.get("processed_articles", [])],
            processed_claims=[str(value) for value in raw.get("processed_claims", [])],
            observations=raw.get("observations") if isinstance(raw.get("observations"), list) else [],
        )

    def _save_probability(self, state: ProbabilityState) -> None:
        _atomic_json_write(self.probability_path, asdict(state))

    def _load_paper(self) -> PaperState:
        if not self.paper_path.exists():
            return self._new_paper()
        raw = json.loads(self.paper_path.read_text(encoding="utf-8"))
        if not self._compatible(raw):
            self._archive_incompatible(self.paper_path)
            return self._new_paper()
        return PaperState(
            updated_at=str(raw.get("updated_at") or ""),
            state_version=int(raw.get("state_version") or 0),
            model_version=str(raw.get("model_version") or ""),
            config_fingerprint=str(raw.get("config_fingerprint") or ""),
            positions=raw.get("positions") if isinstance(raw.get("positions"), dict) else {},
            trades=raw.get("trades") if isinstance(raw.get("trades"), list) else [],
            realized_pnl=float(raw.get("realized_pnl") or 0.0),
        )

    def _save_paper(self, state: PaperState) -> None:
        _atomic_json_write(self.paper_path, asdict(state))

    def _compatible(self, raw: Any) -> bool:
        return (
            isinstance(raw, dict)
            and int(raw.get("state_version") or 0) == FORECAST_STATE_VERSION
            and str(raw.get("model_version") or "") == self.config.forecast.model_version
            and str(raw.get("config_fingerprint") or "") == self.config_fingerprint
        )

    def _archive_incompatible(self, path: Path) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path.replace(path.with_name(f"{path.stem}.incompatible-{stamp}{path.suffix}"))

    def _mark_processed(self, state: ProbabilityState, article_hash: str, claim: str | None) -> None:
        state.processed_articles.append(article_hash)
        state.processed_articles = state.processed_articles[-self.config.forecast.max_processed_articles :]
        if claim is not None:
            state.processed_claims.append(claim)
            state.processed_claims = state.processed_claims[-self.config.forecast.max_processed_articles :]


def _passes_agree(passes: list[LocationSignal]) -> bool:
    first = passes[0]
    fields = (
        "confirmed_location",
        "future_expected_formal_location",
        "forecast_target_location",
        "evidence_direction",
        "qualifies_as_senior_round",
        "evidence_strength",
        "source_tier",
        "final_decision_announced",
    )
    return all(all(getattr(first, field) == getattr(other, field) for field in fields) for other in passes[1:])


def _evidence_target(config: LocationBotConfig, signal: LocationSignal) -> tuple[str | None, str]:
    explicit = signal.forecast_target_location
    if explicit not in {"none", "unclear", "", "other_specific"} and config.outcome(explicit) is not None:
        return explicit, "forecast_claim"
    confirmed = signal.confirmed_location
    if confirmed not in {"none", "unclear", "", "other_specific"} and config.outcome(confirmed) is not None:
        return confirmed, "confirmed"
    future = signal.future_expected_formal_location
    if future not in {"none", "unclear", "", "other_specific"} and config.outcome(future) is not None:
        return future, "future_expected"
    return None, "none"


def _likelihood_ratio(config: LocationBotConfig, signal: LocationSignal, target_kind: str) -> float:
    source = float(config.forecast.source_likelihoods.get(signal.source_tier, 1.0))
    evidence = float(config.forecast.evidence_likelihoods.get(signal.evidence_strength, 1.0))
    strength = source * evidence
    if target_kind != "confirmed":
        strength = 1.0 + (strength - 1.0) * 0.65
    if not signal.final_decision_announced:
        strength = 1.0 + (strength - 1.0) * 0.75
    if not signal.source_is_trusted:
        strength = 1.0 + (strength - 1.0) * 0.35
    strength = max(1.0, min(strength, 20.0))
    likelihood = 1.0 / strength if signal.evidence_direction == "contradictory" else strength
    return max(0.05, min(likelihood, 20.0))


def _bayes_target_update(probabilities: dict[str, float], target: str, likelihood: float) -> dict[str, float]:
    current = _normalized(probabilities)
    prior = current[target]
    denominator = prior * likelihood + (1.0 - prior)
    posterior = (prior * likelihood / denominator) if denominator > 0 else prior
    remainder_before = max(1e-12, 1.0 - prior)
    remainder_after = max(0.0, 1.0 - posterior)
    updated = {
        name: posterior if name == target else value / remainder_before * remainder_after
        for name, value in current.items()
    }
    return _normalized(updated)


def _normalized(values: dict[str, Any]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for name, value in values.items():
        normalized_name = str(name).strip().lower().replace(" ", "_")
        parsed[normalized_name] = parsed.get(normalized_name, 0.0) + max(0.0, float(value))
    total = sum(parsed.values())
    if total <= 0:
        raise ValueError("forecast probability state has zero total probability")
    return {name: value / total for name, value in parsed.items()}


def _claim_fingerprint(target: str, direction: str, quote: str) -> str:
    normalized = re.sub(r"\s+", " ", quote.strip().lower())
    return hashlib.sha256(f"{target}\n{direction}\n{normalized}".encode()).hexdigest()


def _config_fingerprint(config: LocationBotConfig) -> str:
    payload = {
        "event_slug": config.event.slug,
        "forecast": asdict(config.forecast),
        "outcomes": [
            {"name": outcome.name, "condition_id": outcome.condition_id, "yes_token_id": outcome.yes_token_id}
            for outcome in config.outcomes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["FORECAST_STATE_VERSION", "ForecastPaperEngine", "PaperState", "ProbabilityState"]
