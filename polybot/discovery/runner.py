from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from polybot.core.holdings import _atomic_json_write
from polybot.log import log_event

from .allocator import PortfolioAllocator
from .config import DiscoveryConfig, load_discovery_config
from .context import build_rule_analyzer
from .emit import emit_bot_config
from .gamma_universe import context_from_event, fetch_active_events, is_geopolitical_candidate, merge_refresh
from .opportunity import QuoteProviderProtocol, scan_opportunities
from .scorer import correlation_group, grade_market
from .sources import build_source_plan
from .store import DiscoveryStore
from .types import MarketContext, TRADEABLE_STATES


def _load(config_path: Path) -> tuple[DiscoveryConfig, DiscoveryStore, PortfolioAllocator]:
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    allocator = PortfolioAllocator(config.data_dir / "allocations.json", config.allocator)
    # Persist the caps into the ledger so executor-side PortfolioLinks enforce
    # exactly the limits this pipeline run was configured with.
    allocator.write_caps()
    return config, store, allocator


def discover_markets_command(
    config_path: Path,
    *,
    events_fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> int:
    """Stage 1-2: enumerate the active universe, keep geopolitical candidates,
    and build/refresh a durable context record per market. A changed rule hash
    drops the old analysis and demotes the market to RULES_REVIEW_REQUIRED."""
    config, store, _ = _load(config_path)
    events = fetch_active_events(
        limit=config.universe.max_events,
        page_size=config.universe.page_size,
        fetch=events_fetch,
    )
    rejected: Counter[str] = Counter()
    new_count = refreshed = 0
    for event in events:
        candidate, reason = is_geopolitical_candidate(event, config.universe)
        if not candidate:
            rejected[reason.split(":")[0]] += 1
            continue
        fresh = context_from_event(event)
        if fresh is None:
            rejected["unparseable_event"] += 1
            continue
        if fresh.liquidity < config.universe.min_liquidity and fresh.volume < config.universe.min_volume:
            rejected["below_liquidity_and_volume_floor"] += 1
            continue
        existing = store.load_context(fresh.market_id)
        if existing is None:
            store.save_context(fresh)
            new_count += 1
        else:
            store.save_context(merge_refresh(existing, fresh))
            refreshed += 1
    summary = {
        "events_enumerated": len(events),
        "new_contexts": new_count,
        "refreshed_contexts": refreshed,
        "rejected": dict(rejected),
        "total_contexts": len(store.all_contexts()),
    }
    log_event("discovery_universe_scan", **summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def grade_markets_command(config_path: Path, *, analyzer=None) -> int:
    """Stage 3: run the rule/context analyzer where missing, then grade every
    market and assign its state. Two passes so correlation-group counts are
    computed over provisionally eligible markets."""
    config, store, _ = _load(config_path)
    analyzer = analyzer or build_rule_analyzer(config.classifier)
    contexts = store.all_contexts()

    analyzed: list[MarketContext] = []
    analysis_failures = 0
    for context in contexts:
        if context.rule_analysis is None and len(context.rule_text.strip()) >= config.scoring.min_rule_text_chars:
            try:
                analysis = analyzer.analyze(context)
                context = MarketContext.from_dict({**context.as_dict(), "rule_analysis": analysis.as_dict()})
            except Exception as exc:
                analysis_failures += 1
                log_event("discovery_rule_analysis_failed", market_id=context.market_id, error=str(exc))
        analyzed.append(context)

    provisional = [grade_market(context, config.scoring) for context in analyzed]
    group_counts: Counter[str] = Counter(
        correlation_group(context) for context in provisional if context.state in TRADEABLE_STATES
    )
    provisional_states = {context.market_id: context.state for context in provisional}
    states: Counter[str] = Counter()
    for context in analyzed:
        # group_counts semantics are "other tradeable markets in this group":
        # subtract the market's own provisional membership before regrading.
        group = correlation_group(context)
        others = dict(group_counts)
        if provisional_states.get(context.market_id) in TRADEABLE_STATES:
            others[group] = max(0, others.get(group, 0) - 1)
        graded = grade_market(context, config.scoring, group_counts=others)
        store.save_context(graded)
        states[graded.state] += 1
    summary = {"states": dict(states), "analysis_failures": analysis_failures, "correlation_groups": dict(group_counts)}
    log_event("discovery_grading", **summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def plan_sources_command(config_path: Path, market_id: str | None = None) -> int:
    """Stage 4: derive a per-market source plan from the context package for
    every tradeable market (or one named market)."""
    _, store, _ = _load(config_path)
    contexts = store.all_contexts()
    if market_id:
        contexts = [c for c in contexts if c.market_id == market_id]
        if not contexts:
            raise SystemExit(f"unknown market_id {market_id!r}")
    planned, skipped = [], []
    for context in contexts:
        if market_id is None and context.state not in TRADEABLE_STATES:
            continue
        try:
            plan = build_source_plan(context)
        except ValueError as exc:
            skipped.append({"market_id": context.market_id, "reason": str(exc)})
            continue
        store.save_source_plan(plan)
        planned.append({"market_id": context.market_id, "feeds": len(plan.feed_urls), "auto_trade_domains": plan.auto_trade_domains})
    print(json.dumps({"planned": planned, "skipped": skipped}, indent=2, sort_keys=True))
    return 0


def scan_opportunities_command(config_path: Path, *, quotes: QuoteProviderProtocol | None = None) -> int:
    """Stage 5-6: price every eligible outcome against its estimated
    probability, run the result through the portfolio allocator preview, and
    persist the scan for the funnel report."""
    config, store, allocator = _load(config_path)
    contexts = store.all_contexts()
    quotes = quotes or _live_quotes(contexts)
    opportunities = scan_opportunities(contexts, config.opportunity, quotes, allocator)
    payload = [item.as_dict() for item in opportunities]
    _atomic_json_write(config.data_dir / "opportunities.json", {"opportunities": payload})
    executable = [item for item in opportunities if not item.blockers]
    print(
        json.dumps(
            {
                "scanned_outcomes": len(opportunities),
                "executable": [item.as_dict() for item in executable],
                "blocked": Counter(blocker.split(":")[0] for item in opportunities for blocker in item.blockers),
            },
            indent=2,
            sort_keys=True,
            default=dict,
        )
    )
    return 0


def emit_bot_config_command(config_path: Path, market_id: str, out: Path | None = None) -> int:
    """Stage 7 handoff: render a ready-to-review executor config (binary or
    location bot) for one eligible market. The existing engines remain the
    final execution component; nothing is armed by emission."""
    config, store, allocator = _load(config_path)
    context = store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    if context.state not in TRADEABLE_STATES:
        raise SystemExit(f"market {market_id} is {context.state}; only PAPER/LIVE-eligible markets can be emitted")
    plan = store.load_source_plan(market_id)
    if plan is None:
        raise SystemExit(f"market {market_id} has no source plan; run plan-sources first")
    out = out or Path("configs/geopolitics/generated") / f"{_safe(market_id)}.yaml"
    path = emit_bot_config(
        context,
        plan,
        entry_usd=allocator.config.per_order_usd,
        out_path=out,
        ledger_path=str(allocator.state_path),
    )
    print(json.dumps({"written": str(path), "kind": context.kind, "state": context.state}, indent=2))
    return 0


def funnel_report_command(config_path: Path) -> int:
    """Measure the whole opportunity funnel instead of one hand-picked market:
    all -> understandable -> observable -> eligible -> mispriced ->
    executable, plus current portfolio exposure."""
    config, store, allocator = _load(config_path)
    contexts = store.all_contexts()
    scoring = config.scoring
    understandable = [
        c for c in contexts if c.rule_analysis is not None and c.rule_analysis.rule_clarity >= scoring.min_clarity_paper
    ]
    observable = [
        c for c in understandable if c.rule_analysis is not None and c.rule_analysis.evidence_observability >= scoring.min_observability_paper
    ]
    opportunities_raw: list[dict[str, Any]] = []
    opportunities_path = config.data_dir / "opportunities.json"
    if opportunities_path.exists():
        raw = json.loads(opportunities_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("opportunities"), list):
            opportunities_raw = [item for item in raw["opportunities"] if isinstance(item, dict)]
    mispriced = [o for o in opportunities_raw if o.get("tradable_edge") is not None and not any(str(b).startswith("edge_below_minimum") for b in o.get("blockers", [])) and o.get("tradable_edge", 0) >= config.opportunity.min_edge]
    executable = [o for o in opportunities_raw if not o.get("blockers")]
    report = {
        "funnel": {
            "all_markets": len(contexts),
            "understandable_markets": len(understandable),
            "observable_markets": len(observable),
            "paper_eligible": sum(1 for c in contexts if c.state == "PAPER_ELIGIBLE"),
            "live_confirmation_eligible": sum(1 for c in contexts if c.state == "LIVE_CONFIRMATION_ELIGIBLE"),
            "mispriced_outcomes": len(mispriced),
            "executable_opportunities": len(executable),
        },
        "states": dict(Counter(c.state for c in contexts)),
        "top_blockers": dict(Counter(str(b).split(":")[0] for o in opportunities_raw for b in o.get("blockers", []))),
        "portfolio": allocator.snapshot(),
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def run_discovery_command(
    config_path: Path,
    *,
    once: bool = False,
    events_fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
    quotes: QuoteProviderProtocol | None = None,
    analyzer=None,
    notifier=None,
) -> int:
    """Scheduled pipeline loop: discover -> grade -> plan-sources -> scan on
    an interval, alerting (Telegram) on newly LIVE_CONFIRMATION_ELIGIBLE
    markets and newly executable opportunities. Each stage is fault-isolated:
    one bad cycle logs and waits for the next instead of killing the loop."""
    import time

    from polybot.core.notifier import TelegramNotifier

    config, _, _ = _load(config_path)
    notifier = notifier or TelegramNotifier()
    while True:
        try:
            _run_discovery_cycle(config_path, config, events_fetch=events_fetch, quotes=quotes, analyzer=analyzer, notifier=notifier)
        except Exception as exc:
            log_event("discovery_cycle_error", error=str(exc))
            try:
                notifier.notify("Discovery pipeline cycle failed; continuing", error=str(exc))
            except Exception as notify_exc:
                log_event("discovery_notify_failed", error=str(notify_exc))
        if once:
            return 0
        time.sleep(max(60.0, config.schedule.interval_minutes * 60.0))


def _run_discovery_cycle(
    config_path: Path,
    config: DiscoveryConfig,
    *,
    events_fetch,
    quotes,
    analyzer,
    notifier,
) -> None:
    store = DiscoveryStore(config.data_dir)
    previous = _pipeline_state(config)
    discover_markets_command(config_path, events_fetch=events_fetch)
    grade_markets_command(config_path, analyzer=analyzer)
    plan_sources_command(config_path)
    scan_opportunities_command(config_path, quotes=quotes)

    contexts = store.all_contexts()
    live_now = sorted(c.market_id for c in contexts if c.state == "LIVE_CONFIRMATION_ELIGIBLE")
    executable_now = sorted(
        f"{o.get('market_id')}:{o.get('outcome')}"
        for o in _last_scan(config)
        if not o.get("blockers")
    )
    new_live = [m for m in live_now if m not in set(previous.get("live_eligible", []))]
    new_executable = [o for o in executable_now if o not in set(previous.get("executable", []))]
    for market_id in new_live:
        context = store.load_context(market_id)
        notifier.notify(
            "Discovery: market newly LIVE_CONFIRMATION_ELIGIBLE",
            market_id=market_id,
            question=(context.question if context else ""),
            deadline=(context.deadline_iso if context else ""),
            next_step=f"emit-bot-config --market {market_id}",
        )
    for key in new_executable:
        notifier.notify("Discovery: newly executable opportunity", opportunity=key)
    _atomic_json_write(
        config.data_dir / "pipeline_state.json",
        {"live_eligible": live_now, "executable": executable_now},
    )
    log_event(
        "discovery_cycle_complete",
        live_eligible=len(live_now),
        executable=len(executable_now),
        new_live=len(new_live),
        new_executable=len(new_executable),
    )


def _pipeline_state(config: DiscoveryConfig) -> dict[str, Any]:
    path = config.data_dir / "pipeline_state.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _last_scan(config: DiscoveryConfig) -> list[dict[str, Any]]:
    path = config.data_dir / "opportunities.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = raw.get("opportunities") if isinstance(raw, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _live_quotes(contexts: list[MarketContext]) -> QuoteProviderProtocol:
    # The location quote adapter is market-agnostic (token ids in, best bid/ask
    # out over public CLOB books) and already enforces freshness; reused here
    # rather than duplicated.
    from polybot.location.quotes import PublicClobQuoteAdapter

    token_ids = [
        outcome.yes_token_id
        for context in contexts
        if context.state in TRADEABLE_STATES
        for outcome in context.outcomes
        if outcome.yes_token_id
    ]
    return PublicClobQuoteAdapter(token_ids)


def _safe(market_id: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "-", market_id)[:120]
