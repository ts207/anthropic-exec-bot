from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.log import log_event

from polybot.iran.executor import DryRunTradingAdapter, LiveClobTradingAdapter, TradingAdapter
from polybot.iran.notifier import TelegramNotifier
from polybot.iran.operator import OperatorGate
from polybot.iran.runner import ClassifierBudgetStore, _article_age_hours, _is_feed_summary
from polybot.iran.source_fetcher import ArticleStore, fetch_article, fetch_feed_articles, fetch_listing_article_urls, promote_feed_article
from polybot.iran.storage import StateStore, append_jsonl
from polybot.iran.types import Article
from polybot.iran.verifier import quote_in_article

from .classifier import build_location_classifier
from .config import LocationBotConfig, load_location_config
from .decision import LocationDecision, classify_agreement, final_decision, time_decay_decision
from .executor import TERMINAL_STATES, LocationExecutor
from .market_verifier import verify_all_outcomes, verify_location_event


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    normalized = domain.lower().removeprefix("www.")
    return any(normalized == item or normalized.endswith("." + item) for item in allowed)


def inspect_location_command(config_path: Path) -> int:
    config = load_location_config(config_path)
    held = config.held_outcome()
    result: dict[str, Any] = {
        "config": str(config_path),
        "event": {
            "slug": config.event.slug,
            "question": config.event.question,
            "deadline_date": config.event.deadline_date,
            "held_location": config.event.held_location,
        },
        "held_outcome": {
            "name": held.name,
            "label": held.label,
            "condition_id": held.condition_id,
            "yes_token_id": held.yes_token_id,
            "no_token_id": held.no_token_id,
        },
        "rotation_targets": [o.name for o in config.rotation_targets()],
        "all_outcomes": [o.name for o in config.outcomes],
        "classifier": {"provider": config.classifier.provider, "model": config.classifier.model},
    }
    try:
        verification = verify_location_event(config)
        result["market_verification"] = verification.as_dict()
    except Exception as exc:
        result["market_verification_error"] = str(exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "\nManual confirmation required before live mode. Review market_verification above and "
        "pin event.expected_rule_text_sha256 in config after review."
    )
    return 0


def preflight_location_command(config_path: Path, live_flag: bool = False) -> int:
    config = load_location_config(config_path)
    held = config.held_outcome()
    adapter = _live_adapter() if live_flag else DryRunTradingAdapter()
    gate = OperatorGate(config_path, config)
    status = gate.status(live_requested=live_flag)
    result: dict[str, Any] = {
        "status": "blocked" if status.blockers else "ok",
        "operator": status.as_dict(),
        "config": str(config_path),
        "held_outcome": held.name,
    }
    try:
        position = adapter.query_live_position(held.yes_token_id, held.no_token_id)
        result["live_position"] = {"yes_shares": position.yes_shares, "no_shares": position.no_shares}
    except Exception as exc:
        result["live_position_error"] = str(exc)
        status.blockers.append("live_position_query_failed")
    try:
        verification = verify_location_event(config)
        verify_all_outcomes(config, verification, require_tradeable=live_flag)
        result["market_verification"] = verification.as_dict()
    except Exception as exc:
        result["market_verification_error"] = str(exc)
        status.blockers.append("market_verification_failed")
    result["status"] = "blocked" if status.blockers else "ok"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


def ack_location_live_command(config_path: Path, note: str = "") -> int:
    config = load_location_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.write_ack(note=note), indent=2, sort_keys=True))
    return 0


def set_location_mode_command(config_path: Path, mode: str) -> int:
    config = load_location_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.set_position_mode(mode), indent=2, sort_keys=True))
    return 0


def smoke_location_classifier_command(
    config_path: Path,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str = "classifier smoke",
    domain: str = "reuters.com",
) -> int:
    if not url and not text:
        raise SystemExit("smoke-location-classifier requires --url or --text")
    config = load_location_config(config_path)
    article = fetch_article(url, SETTINGS.user_agent) if url else Article(
        url="smoke://local",
        domain=domain,
        title=title,
        published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=text or "",
        hash=f"smoke:{hash(text or '')}",
    )
    classifier = build_location_classifier(config)
    try:
        factors = classifier.classify(article, config.event.resolution_rules)
    except Exception as exc:
        print(json.dumps({"article": article.__dict__, "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    decision = final_decision(config, factors)
    print(
        json.dumps(
            {
                "article": article.__dict__,
                "factors": asdict(factors),
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "target_outcome": decision.target_outcome,
                },
                "ok": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def run_location_command(config_path: Path, live_flag: bool) -> int:
    config = load_location_config(config_path)
    if not config.sources.poll_urls and not config.sources.feed_urls and not config.time_decay.enabled:
        raise SystemExit("no sources configured and time_decay is disabled")
    if not config.execution.dry_run and not live_flag:
        raise SystemExit("execution.dry_run=false requires --live")
    if live_flag and config.execution.dry_run:
        raise SystemExit("--live requires execution.dry_run=false")
    verification = verify_location_event(config)
    verify_all_outcomes(config, verification, require_tradeable=live_flag)
    if live_flag:
        held = config.held_outcome()
        held_verification = verification.outcomes.get(held.name)
        if held_verification is None or not held_verification.tradeable:
            raise SystemExit("held outcome market is not active/open/accepting orders; refusing live execution")
    adapter = _live_adapter() if live_flag else DryRunTradingAdapter()
    gate = OperatorGate(config_path, config)
    bot = LocationProtectionBot(config=config, adapter=adapter, operator_gate=gate, live_requested=live_flag)
    while True:
        try:
            bot.run_once()
        except Exception as exc:
            log_event("location_run_once_error", error=str(exc))
            try:
                bot.notifier.notify("Location protection polling cycle failed; continuing", error=str(exc))
            except Exception as notify_exc:
                log_event("location_notify_failed", error=str(notify_exc))
        time.sleep(config.safety.poll_seconds)


def _live_adapter() -> TradingAdapter:
    return LiveClobTradingAdapter()


class LocationProtectionBot:
    def __init__(self, *, config: LocationBotConfig, adapter: TradingAdapter, operator_gate: OperatorGate | None = None, live_requested: bool = False):
        self.config = config
        self.store = StateStore(config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir)
        self.article_store = ArticleStore(config.logs_dir / "location_articles.jsonl")
        self.notifier = TelegramNotifier()
        self.classifier = build_location_classifier(config)
        self.classifier_budget = ClassifierBudgetStore(self.store.data_dir)
        self.executor = LocationExecutor(config, self.store, self.notifier, adapter)
        self.operator_gate = operator_gate
        self.live_requested = live_requested

    def run_once(self) -> list[LocationDecision]:
        if self.operator_gate is not None and self.operator_gate.current_mode() == "off":
            log_event("location_operator_off_cycle_skip")
            return []
        self._process_monitoring()
        decisions: list[LocationDecision] = []
        decay = time_decay_decision(self.config)
        if decay.action != "NO_ACTION" and self._decay_still_actionable(decay):
            decisions.append(self._execute_if_allowed(decay, _synthetic_article(decay.reason)))
        for url in self.config.sources.poll_urls:
            for article in self._fetch_poll_articles(url):
                if not self.article_store.store(article):
                    continue
                decisions.append(self.process_article(article, always_notify=True))
        for feed_url in self.config.sources.feed_urls:
            try:
                articles = fetch_feed_articles(
                    feed_url,
                    SETTINGS.user_agent,
                    include_terms=self.config.sources.feed_include_terms,
                    exclude_terms=self.config.sources.feed_exclude_terms,
                    limit=self.config.sources.max_feed_entries_per_cycle,
                )
            except Exception as exc:
                log_event("location_feed_fetch_error", url=feed_url, error=str(exc))
                continue
            for article in articles:
                if not self.article_store.store(article):
                    continue
                decisions.append(self.process_article(article))
                promoted = self._promote_feed_article(article)
                if promoted is None or not self.article_store.store(promoted):
                    continue
                decisions.append(self.process_article(promoted))
        return decisions

    def _decay_still_actionable(self, decay: LocationDecision) -> bool:
        """Suppress re-executing a time-decay decision every polling cycle.

        The decay decision recurs on every run_once after its date passes; the
        executor's dedupe branches would otherwise notify (Telegram) each cycle
        (every safety.poll_seconds) once the trim/exit has already happened.
        """
        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES:
            log_event("location_time_decay_skip_terminal", state=current.state, action=decay.action)
            return False
        if decay.action == "TRIM_YES" and (
            (current is not None and current.state == "TRIMMED") or self.store.marker("TRIMMED") is not None
        ):
            log_event("location_time_decay_skip_already_trimmed", action=decay.action)
            return False
        return True

    def process_article(self, article: Article, *, always_notify: bool = False) -> LocationDecision:
        if always_notify:
            for chunk in _chunk_telegram_message(_format_live_update_message(article)):
                self.notifier.notify(chunk, title=article.title, url=article.url, domain=article.domain, published_at=article.published_at)
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            decision = LocationDecision("ALERT_ONLY", "3", f"article_stale_skipped_classification:{age_hours:.0f}h")
            self._log_decision(article, decision)
            return decision
        if _is_feed_summary(article) and not self.config.classifier.classify_feed_summaries:
            decision = LocationDecision("ALERT_ONLY", "3", "feed_summary_classification_disabled")
            self._log_decision(article, decision)
            self.notifier.notify("Location protection feed summary skipped classifier", reason=decision.reason, url=article.url)
            return decision
        block_reason = self.classifier_budget.block_reason(self.config.classifier)
        if block_reason is not None:
            decision = LocationDecision("ALERT_ONLY", "3", block_reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(block_reason)
            return decision
        try:
            passes = [self._classify_with_budget(article, index) for index in range(max(1, self.config.classifier.passes))]
        except Exception as exc:
            self.classifier_budget.record_error()
            decision = LocationDecision("ALERT_ONLY", "3", f"classifier_error:{exc}")
            self._log_decision(article, decision)
            self.notifier.notify("Location classifier unavailable or failed; no trade", error=str(exc))
            return decision
        if self.config.classifier.require_pass_agreement:
            decision = classify_agreement(self.config, passes)
        else:
            decision = final_decision(self.config, passes[0])
        decision = self._verify_quote_or_alert(decision, article)
        decision = self._enforce_source_policy(article, decision)
        decision = self._enforce_execution_policy(decision)
        self._log_decision(article, decision)
        if decision.action == "ALERT_ONLY":
            self.notifier.notify("Location protection alert only; no trade", level=decision.level, reason=decision.reason, url=article.url)
        return self._execute_if_allowed(decision, article)

    def _verify_quote_or_alert(self, decision: LocationDecision, article: Article) -> LocationDecision:
        if decision.factors is None or decision.level not in {"4A", "4B"}:
            return decision
        if quote_in_article(decision.factors.quote_supporting_trigger, article.raw_text):
            return decision
        return LocationDecision("ALERT_ONLY", "3", "quote_verification_failed", decision.target_outcome, decision.factors)

    def _promote_feed_article(self, article: Article) -> Article | None:
        if article.source_kind != "feed" or not self.config.sources.promote_feed_to_article:
            return None
        promoted = promote_feed_article(article, SETTINGS.user_agent)
        if promoted is None:
            log_event("location_feed_promotion_failed", url=article.url, domain=article.domain)
            return None
        return promoted

    def _enforce_source_policy(self, article: Article, decision: LocationDecision) -> LocationDecision:
        if decision.action not in {"TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES"}:
            return decision
        # promoted_feed_summary is feed-derived text (publisher fetch failed),
        # so it is gated by the same flag as raw feed items.
        if article.source_kind in {"feed", "promoted_feed_summary"} and not self.config.sources.allow_feed_auto_trade:
            return LocationDecision("ALERT_ONLY", "3", "feed_item_auto_trade_disabled", decision.target_outcome, decision.factors)
        # Freshness gate (ported from the iran runner): stale items can alert
        # but not trade -- old news is already priced in, and with feed
        # auto-trade enabled a stale feed item must never fire a sale.
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            return LocationDecision("ALERT_ONLY", "3", f"article_stale_for_auto_trade:{age_hours:.0f}h", decision.target_outcome, decision.factors)
        if domain_allowed(article.domain, self.config.sources.alert_only_domains):
            return LocationDecision("ALERT_ONLY", "3", "source_domain_alert_only", decision.target_outcome, decision.factors)
        if not domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            return LocationDecision("ALERT_ONLY", "3", "source_domain_not_auto_trade", decision.target_outcome, decision.factors)
        return decision

    def _enforce_execution_policy(self, decision: LocationDecision) -> LocationDecision:
        if decision.action not in {"TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES"}:
            return decision
        if not self.config.trigger.trusted_single_source_execution:
            return LocationDecision("ALERT_ONLY", "3", "single_source_execution_disabled", decision.target_outcome, decision.factors)
        if not _level_meets_threshold(decision.level, self.config.trigger.auto_execute_level):
            return LocationDecision("ALERT_ONLY", "3", "below_auto_execute_level", decision.target_outcome, decision.factors)
        if self.config.safety.max_executions <= 0:
            return LocationDecision("ALERT_ONLY", "3", "max_executions_zero", decision.target_outcome, decision.factors)
        return decision

    def _execute_if_allowed(self, decision: LocationDecision, article: Article) -> LocationDecision:
        if self.operator_gate is not None:
            # OperatorGate.check/log_block_once only read `.action` off the
            # object passed in, so LocationDecision satisfies it structurally
            # without needing the iran-specific Decision dataclass.
            gate_result = self.operator_gate.check(decision, live_requested=self.live_requested)
            if not gate_result.allowed:
                if self.operator_gate.log_block_once(gate_result, decision):
                    self.notifier.notify(
                        "Location protection execution blocked by operator gate",
                        action=decision.action,
                        mode=gate_result.mode,
                        reason=gate_result.reason,
                        url=article.url,
                    )
                return LocationDecision("ALERT_ONLY", "3", f"operator_block:{gate_result.reason}", decision.target_outcome, decision.factors)
        self.executor.execute(decision, article)
        return decision

    def _fetch_poll_articles(self, url: str) -> list[Article]:
        if _is_listing_url(url):
            try:
                article_urls = fetch_listing_article_urls(url, SETTINGS.user_agent, limit=self.config.sources.max_feed_entries_per_cycle)
            except Exception as exc:
                log_event("location_listing_fetch_error", url=url, error=str(exc))
                article_urls = []
            if article_urls:
                log_event("location_listing_articles_discovered", url=url, count=len(article_urls))
                articles: list[Article] = []
                for article_url in article_urls:
                    try:
                        articles.append(fetch_article(article_url, SETTINGS.user_agent))
                    except Exception as exc:
                        log_event("location_listing_article_fetch_error", listing_url=url, url=article_url, error=str(exc))
                return articles
        try:
            return [fetch_article(url, SETTINGS.user_agent)]
        except Exception as exc:
            log_event("location_source_fetch_error", url=url, error=str(exc))
            return []

    def _process_monitoring(self) -> None:
        self._process_price_alerts()
        self._process_heartbeat()

    def _process_price_alerts(self) -> None:
        price_config = self.config.monitoring.price_alerts
        if not price_config.enabled or not price_config.thresholds:
            return
        outcome = self.config.outcome(price_config.outcome or self.config.event.held_location)
        if outcome is None:
            log_event("location_price_alert_skip", reason="outcome_not_found", outcome=price_config.outcome)
            return
        bid = self.executor.adapter.yes_best_bid(outcome.yes_token_id)
        ask = self.executor.adapter.yes_best_ask(outcome.yes_token_id)
        price = _monitor_price(bid, ask)
        if price is None:
            log_event("location_price_alert_skip", reason="price_unavailable", outcome=outcome.name)
            return
        state = self._monitoring_state()
        price_state = state.setdefault("price_alerts", {})
        key = outcome.name
        previous_raw = price_state.get(key, {}).get("last_price") if isinstance(price_state.get(key), dict) else None
        previous = _as_float(previous_raw)
        crossed = [
            threshold
            for threshold in sorted(float(item) for item in price_config.thresholds)
            if previous is not None and _crossed(previous, price, threshold)
        ]
        price_state[key] = {
            "last_price": price,
            "yes_best_bid": bid,
            "yes_best_ask": ask,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_monitoring_state(state)
        for threshold in crossed:
            direction = "up" if price > previous else "down"
            self.notifier.notify(
                "Location price band crossed",
                outcome=outcome.label,
                threshold=threshold,
                direction=direction,
                price=price,
                yes_best_bid=bid,
                yes_best_ask=ask,
            )
            log_event("location_price_band_crossed", outcome=outcome.name, threshold=threshold, direction=direction, price=price)

    def _process_heartbeat(self) -> None:
        heartbeat = self.config.monitoring.heartbeat
        if not heartbeat.enabled:
            return
        state = self._monitoring_state()
        raw = state.get("heartbeat")
        last_sent = _parse_iso(raw.get("last_sent_at")) if isinstance(raw, dict) else None
        now = datetime.now(timezone.utc)
        interval_seconds = max(1.0, heartbeat.interval_hours * 3600.0)
        if last_sent is not None and (now - last_sent).total_seconds() < interval_seconds:
            return
        held = self.config.held_outcome()
        bid = self.executor.adapter.yes_best_bid(held.yes_token_id)
        ask = self.executor.adapter.yes_best_ask(held.yes_token_id)
        budget = self.classifier_budget.status(self.config.classifier)
        self.notifier.notify(
            "Location protection heartbeat",
            held_outcome=held.label,
            dry_run=self.config.execution.dry_run,
            yes_best_bid=bid,
            yes_best_ask=ask,
            classifier_budget=budget,
        )
        state["heartbeat"] = {"last_sent_at": now.isoformat()}
        self._write_monitoring_state(state)
        log_event("location_heartbeat_sent", held_outcome=held.name, yes_best_bid=bid, yes_best_ask=ask)

    def _monitoring_state_path(self) -> Path:
        return self.store.data_dir / "monitoring.json"

    def _monitoring_state(self) -> dict[str, Any]:
        path = self._monitoring_state_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_monitoring_state(self, state: dict[str, Any]) -> None:
        path = self._monitoring_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    def _log_decision(self, article: Article, decision: LocationDecision) -> None:
        append_jsonl(
            self.config.logs_dir / "location_decisions.jsonl",
            {
                "article": article.__dict__,
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "target_outcome": decision.target_outcome,
                    "factors": decision.factors.__dict__ if decision.factors else None,
                },
            },
        )

    def _classify_with_budget(self, article: Article, pass_index: int) -> Any:
        context = self.config.event.resolution_rules
        input_chars = len(article.title) + len(article.raw_text) + len(context)
        telemetry = {
            "provider": self.config.classifier.provider,
            "model": self.config.classifier.model,
            "pass_index": pass_index,
            "article_hash": article.hash,
            "source_kind": article.source_kind,
            "domain": article.domain,
            "input_char_count": input_chars,
            "estimated_input_tokens": max(1, input_chars // 4),
        }
        self.classifier_budget.record_attempt()
        log_event("location_classifier_attempt", **telemetry)
        if hasattr(self.classifier, "last_usage"):
            setattr(self.classifier, "last_usage", None)
        factors = self.classifier.classify(article, context)
        usage = getattr(self.classifier, "last_usage", None)
        if isinstance(usage, dict):
            telemetry["usage"] = usage
        log_event("location_classifier_result", **telemetry)
        return factors

    def _notify_classifier_budget_block_once(self, reason: str) -> None:
        window = "hour" if reason in {"classifier_budget_exhausted_hourly", "classifier_error_cap_exceeded"} else "day"
        if self.classifier_budget.mark_notified_once(reason, window):
            self.notifier.notify("Location protection classifier budget blocked classification", reason=reason)


def _synthetic_article(reason: str) -> Article:
    return Article(
        url="time-decay://local",
        domain="time-decay.local",
        title=reason,
        published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=reason,
        hash=f"time-decay:{reason}",
    )


def _level_meets_threshold(level: str, threshold: int) -> bool:
    if level == "TIME":
        return True
    digits = "".join(ch for ch in level if ch.isdigit())
    if not digits:
        return False
    return int(digits) >= threshold


def _is_listing_url(url: str) -> bool:
    return "/tag/" in url or "/topics/" in url


def _monitor_price(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 4)
    return bid if bid is not None else ask


def _crossed(previous: float, current: float, threshold: float) -> bool:
    return (previous < threshold <= current) or (previous > threshold >= current)


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


_TELEGRAM_CHUNK_CHARS = 3500


def _format_live_update_message(article: Article) -> str:
    parts = [article.title.strip(), "", article.raw_text.strip(), "", f"Source: {article.url}"]
    return "\n".join(part for part in parts if part)


def _chunk_telegram_message(message: str, limit: int = _TELEGRAM_CHUNK_CHARS) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    remaining = message
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    total = len(chunks)
    return [f"[{i + 1}/{total}]\n{chunk}" for i, chunk in enumerate(chunks)]
