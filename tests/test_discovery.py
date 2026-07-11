from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.binary.config import load_binary_config
from polybot.discovery.allocator import AllocationRequest, PortfolioAllocator
from polybot.discovery.config import AllocatorConfig, DiscoveryConfig, OpportunityConfig, ScoringConfig, UniverseConfig
from polybot.discovery.context import FixtureRuleAnalyzer
from polybot.discovery.emit import emit_bot_config
from polybot.discovery.gamma_universe import context_from_event, is_geopolitical_candidate, merge_refresh
from polybot.discovery.opportunity import scan_opportunities, tradable_edge
from polybot.discovery.runner import (
    discover_markets_command,
    emit_bot_config_command,
    funnel_report_command,
    grade_markets_command,
    plan_sources_command,
    scan_opportunities_command,
)
from polybot.discovery.scorer import grade_market
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import MarketContext
from polybot.location.config import load_location_config


RULES = (
    "This market will resolve YES if senior representatives of the United States and Iran "
    "convene a formal round of peace talks negotiations before the deadline. "
    "Technical, staff-level, working-group, or preparatory meetings will not count. "
    "If no qualifying round begins by the deadline, this market resolves NO. "
    "Resolution source: a consensus of credible reporting including reuters.com. "
    "Brief greetings, chance encounters, or photo opportunities do not count and will not qualify."
) * 3


def _market(slug: str, question: str, *, group_title: str = "", liquidity: float = 8000.0, volume: float = 50000.0, description: str = RULES, closed: bool = False) -> dict:
    return {
        "slug": slug,
        "question": question,
        "groupItemTitle": group_title,
        "conditionId": f"0x{slug}",
        "clobTokenIds": json.dumps([f"{slug}-yes", f"{slug}-no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.4", "0.6"]),
        "description": description,
        "resolutionSource": "reuters.com",
        "negRisk": bool(group_title),
        "active": not closed,
        "closed": closed,
        "acceptingOrders": not closed,
        "volume": volume,
        "liquidity": liquidity,
        "endDate": "2026-09-30T23:59:00Z",
    }


def _grouped_event() -> dict:
    return {
        "slug": "us-iran-talks-location",
        "title": "Where will the next US-Iran peace talks be?",
        "description": RULES,
        "endDate": "2026-09-30T23:59:00Z",
        "negRisk": True,
        "tags": [{"label": "Geopolitics"}],
        "markets": [
            _market("talks-qatar", "Will the talks be in Qatar?", group_title="Qatar"),
            _market("talks-oman", "Will the talks be in Oman?", group_title="Oman"),
        ],
    }


def _binary_event(slug: str = "iran-ceasefire", *, liquidity: float = 9000.0, volume: float = 50000.0, description: str = RULES, closed: bool = False) -> dict:
    return {
        "slug": slug,
        "title": "Will the United States and Iran hold peace talks by September 30?",
        "endDate": "2026-09-30T23:59:00Z",
        "tags": ["politics"],
        "markets": [_market(f"{slug}-m", "Will the United States and Iran hold peace talks negotiations by September 30?", liquidity=liquidity, volume=volume, description=description, closed=closed)],
    }


def _sports_event() -> dict:
    return {
        "slug": "nba-finals",
        "title": "Who wins the NBA finals?",
        "tags": ["sports"],
        "markets": [_market("nba-m", "Who wins the NBA finals?")],
    }


def _universe() -> UniverseConfig:
    return UniverseConfig(min_liquidity=500.0, min_volume=1000.0)


# ---- universe filter + context ----


def test_geopolitical_filter_accepts_tags_and_keywords() -> None:
    ok, reason = is_geopolitical_candidate(_grouped_event(), _universe())
    assert ok and reason.startswith("tag_match")
    ok, reason = is_geopolitical_candidate(_binary_event(), _universe())
    assert ok
    ok, reason = is_geopolitical_candidate(_sports_event(), _universe())
    assert not ok
    assert reason == "excluded_keyword:nba"


def test_context_from_grouped_event() -> None:
    context = context_from_event(_grouped_event())
    assert context is not None
    assert context.kind == "grouped"
    assert context.market_id == "us-iran-talks-location"
    assert [o.name for o in context.outcomes] == ["qatar", "oman"]
    assert context.outcomes[0].yes_token_id == "talks-qatar-yes"
    assert context.rule_text_sha256


def test_context_from_binary_event() -> None:
    context = context_from_event(_binary_event())
    assert context is not None
    assert context.kind == "binary"
    assert context.market_id == "0xiran-ceasefire-m"
    assert len(context.outcomes) == 1


def test_rule_change_drops_analysis_and_demotes() -> None:
    original = context_from_event(_binary_event())
    assert original is not None
    analyzed = MarketContext.from_dict({**original.as_dict(), "rule_analysis": FixtureRuleAnalyzer().analyze(original).as_dict(), "state": "LIVE_CONFIRMATION_ELIGIBLE"})
    fresh = context_from_event(_binary_event(description=RULES + " AMENDED."))
    assert fresh is not None
    merged = merge_refresh(analyzed, fresh)
    assert merged.rule_version == 2
    assert merged.rule_analysis is None
    assert merged.state == "RULES_REVIEW_REQUIRED"
    assert merged.state_reasons == ["rule_text_changed"]


# ---- analyzer + scorer ----


def test_fixture_analyzer_detects_parties_and_families() -> None:
    context = context_from_event(_binary_event())
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    assert "united_states" in analysis.parties
    assert "iran" in analysis.parties
    assert analysis.rule_clarity > 0.5
    assert not analysis.discretionary
    assert "talks" in " ".join(analysis.keywords)


def test_fixture_analyzer_flags_discretion() -> None:
    context = context_from_event(_binary_event(description=RULES + " The committee may resolve at its sole discretion."))
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    assert analysis.discretionary
    assert analysis.automation_suitability == 0.0


def _analyzed_context(event: dict) -> MarketContext:
    context = context_from_event(event)
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    return MarketContext.from_dict({**context.as_dict(), "rule_analysis": analysis.as_dict()})


def test_scorer_live_eligible_and_paper_downgrades() -> None:
    scoring = ScoringConfig()
    live = grade_market(_analyzed_context(_binary_event()), scoring)
    assert live.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert live.correlation_group == "iran|united_states"

    thin = grade_market(_analyzed_context(_binary_event(liquidity=800.0)), scoring)
    assert thin.state == "PAPER_ELIGIBLE"
    assert any(reason.startswith("liquidity_below_live_threshold") for reason in thin.state_reasons)


def test_scorer_hard_states() -> None:
    scoring = ScoringConfig()
    closed = grade_market(_analyzed_context(_binary_event(closed=True)), scoring)
    assert closed.state == "CLOSED"

    context = context_from_event(_binary_event(description="short"))
    assert context is not None
    short_rules = grade_market(context, scoring)
    assert short_rules.state == "RULES_REVIEW_REQUIRED"
    assert short_rules.state_reasons == ["missing_or_short_resolution_rules"]

    unanalyzed = grade_market(context_from_event(_binary_event()), scoring)  # type: ignore[arg-type]
    assert unanalyzed.state == "RULES_REVIEW_REQUIRED"
    assert unanalyzed.state_reasons == ["rule_analysis_missing"]

    discretionary = grade_market(_analyzed_context(_binary_event(description=RULES + " sole discretion.")), scoring)
    assert discretionary.state == "MONITOR_ONLY"


def test_scorer_correlation_group_limit_downgrades_live() -> None:
    scoring = ScoringConfig(max_markets_per_correlation_group=1)
    context = _analyzed_context(_binary_event())
    graded = grade_market(context, scoring, group_counts={"iran|united_states": 1})
    assert graded.state == "PAPER_ELIGIBLE"
    assert any(reason.startswith("correlation_group_limit") for reason in graded.state_reasons)


# ---- source plan ----


def test_source_plan_requires_analysis_and_derives_sources() -> None:
    unanalyzed = context_from_event(_binary_event())
    assert unanalyzed is not None
    with pytest.raises(ValueError, match="no rule analysis"):
        build_source_plan(unanalyzed)

    plan = build_source_plan(_analyzed_context(_binary_event()))
    assert "reuters.com" in plan.auto_trade_domains
    assert "state.gov" in plan.auto_trade_domains  # united_states official domain
    assert any("news.google.com/rss" in url for url in plan.feed_urls)
    assert plan.escalate_terms
    assert plan.rationale


# ---- allocator ----


def _allocator(tmp_path: Path, **overrides) -> PortfolioAllocator:
    config = AllocatorConfig(**overrides) if overrides else AllocatorConfig()
    return PortfolioAllocator(tmp_path / "allocations.json", config)


def _request(usd: float = 50.0, market: str = "m1", group: str = "g1") -> AllocationRequest:
    return AllocationRequest(market_id=market, event_slug="e1", correlation_group=group, deadline_iso="2026-09-30T23:59:00Z", usd=usd)


def test_allocator_grants_within_caps_and_commits(tmp_path) -> None:
    allocator = _allocator(tmp_path)
    granted, blockers = allocator.preview(_request())
    assert granted == 50.0 and not blockers
    allocator.commit(_request())
    granted, blockers = allocator.preview(_request())
    assert granted == 50.0 and not blockers  # per-market 100 cap leaves 50
    allocator.commit(_request())
    granted, blockers = allocator.preview(_request())
    assert granted == 0.0
    assert "per_market_limit" in blockers


def test_allocator_max_positions_and_release(tmp_path) -> None:
    allocator = _allocator(tmp_path, max_open_positions=1, per_event_usd=1000.0, per_group_usd=1000.0, daily_usd=1000.0, total_usd=1000.0, per_market_usd=1000.0)
    allocator.commit(_request(market="m1"))
    _, blockers = allocator.preview(_request(market="m2", group="g2"))
    assert "max_open_positions" in blockers
    allocator.release_position("m1")
    granted, blockers = allocator.preview(_request(market="m2", group="g2"))
    assert granted == 50.0 and not blockers


def test_allocator_group_concentration(tmp_path) -> None:
    allocator = _allocator(tmp_path, per_group_usd=60.0, per_market_usd=1000.0, per_event_usd=1000.0)
    allocator.commit(_request(usd=50.0, market="m1", group="iran|united_states"))
    granted, blockers = allocator.preview(_request(usd=50.0, market="m2", group="iran|united_states"))
    assert granted == 10.0 and not blockers


# ---- opportunity engine ----


class _FakeQuotes:
    def __init__(self, ask: float | None = 0.40, bid: float | None = 0.38):
        self.ask, self.bid = ask, bid

    def yes_best_ask(self, token_id: str) -> float | None:
        return self.ask

    def yes_best_bid(self, token_id: str) -> float | None:
        return self.bid


def test_tradable_edge_accounting() -> None:
    config = OpportunityConfig()
    assert tradable_edge(0.60, 0.40, config) == pytest.approx(0.14)


def _graded(event: dict) -> MarketContext:
    return grade_market(_analyzed_context(event), ScoringConfig())


def test_scan_finds_executable_opportunity(tmp_path) -> None:
    context = _graded(_binary_event())
    config = OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.60}})
    results = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path))
    assert len(results) == 1
    opp = results[0]
    assert not opp.blockers
    assert opp.tradable_edge == pytest.approx(0.14)
    assert opp.allocation_usd == 50.0


def test_scan_blockers(tmp_path) -> None:
    context = _graded(_binary_event())
    allocator = _allocator(tmp_path)
    no_estimate = scan_opportunities([context], OpportunityConfig(), _FakeQuotes(), allocator)
    assert no_estimate[0].blockers == ["no_probability_estimate"]

    config = OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.60}})
    thin_edge = scan_opportunities([context], config, _FakeQuotes(ask=0.58, bid=0.56), allocator)
    assert any(b.startswith("edge_below_minimum") for b in thin_edge[0].blockers)

    wide = scan_opportunities([context], config, _FakeQuotes(ask=0.40, bid=0.10), allocator)
    assert any(b.startswith("spread_above_limit") for b in wide[0].blockers)

    pricey = scan_opportunities([context], OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.99}}), _FakeQuotes(ask=0.95, bid=0.94), allocator)
    assert any(b.startswith("price_above_cap") for b in pricey[0].blockers)


# ---- config emission round-trips through the real executor loaders ----


def test_emit_binary_config_loads(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    out = emit_bot_config(context, plan, entry_usd=50.0, out_path=tmp_path / "binary.yaml")
    config = load_binary_config(out)
    assert config.entry.enabled and config.entry.side == "YES"
    assert config.market.expected_rule_text_sha256 == context.rule_text_sha256
    assert config.execution.dry_run is True
    assert config.sources.feed_urls == plan.feed_urls


def test_emit_location_config_loads(tmp_path) -> None:
    context = _graded(_grouped_event())
    plan = build_source_plan(context)
    out = emit_bot_config(context, plan, entry_usd=50.0, out_path=tmp_path / "location.yaml")
    config = load_location_config(out)
    assert config.entry.enabled
    assert config.entry_target_names() == {"qatar", "oman"}
    assert config.event.expected_rule_text_sha256 == context.rule_text_sha256
    assert config.execution.dry_run is True


def test_emit_rejects_stale_source_plan(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    stale = plan.from_dict({**plan.as_dict(), "rule_text_sha256": "different"})
    with pytest.raises(ValueError, match="different rule-text version"):
        emit_bot_config(context, stale, entry_usd=50.0, out_path=tmp_path / "x.yaml")


# ---- full pipeline through the CLI commands ----


def _pipeline_config(tmp_path: Path) -> Path:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        f"""
classifier:
  provider: rule_based
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return path


def test_full_pipeline(tmp_path, capsys) -> None:
    config_path = _pipeline_config(tmp_path)
    events = [_grouped_event(), _binary_event(), _sports_event(), _binary_event(slug="thin", liquidity=100.0, volume=200.0)]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    assert discover_markets_command(config_path, events_fetch=fetch) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["new_contexts"] == 2  # sports excluded, thin below floors
    assert summary["rejected"]["excluded_keyword"] == 1

    assert grade_markets_command(config_path) == 0
    graded = json.loads(capsys.readouterr().out)
    assert graded["states"]["LIVE_CONFIRMATION_ELIGIBLE"] == 2

    assert plan_sources_command(config_path) == 0
    planned = json.loads(capsys.readouterr().out)
    assert len(planned["planned"]) == 2

    store = DiscoveryStore(tmp_path / "data")
    binary_id = "0xiran-ceasefire-m"
    # Give the binary market a probability estimate so the scan can price it.
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"""
opportunity:
  probability_estimates:
    "{binary_id}":
      "yes": 0.60
""",
        encoding="utf-8",
    )
    assert scan_opportunities_command(config_path, quotes=_FakeQuotes()) == 0
    scan = json.loads(capsys.readouterr().out)
    assert scan["scanned_outcomes"] == 3  # 2 grouped legs + 1 binary
    assert len(scan["executable"]) == 1
    assert scan["executable"][0]["market_id"] == binary_id

    out_path = tmp_path / "generated" / "binary.yaml"
    assert emit_bot_config_command(config_path, binary_id, out=out_path) == 0
    capsys.readouterr()
    assert load_binary_config(out_path).market.expected_rule_text_sha256 == store.load_context(binary_id).rule_text_sha256

    assert funnel_report_command(config_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["funnel"]["all_markets"] == 2
    assert report["funnel"]["understandable_markets"] == 2
    assert report["funnel"]["live_confirmation_eligible"] == 2
    assert report["funnel"]["executable_opportunities"] == 1
