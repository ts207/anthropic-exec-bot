from __future__ import annotations

from datetime import datetime, timezone

from .registry import WIRE_DOMAINS, google_news_rss, official_domains
from .types import MarketContext, SourcePlan


def build_source_plan(context: MarketContext) -> SourcePlan:
    """Derive a per-market source plan FROM the context package.

    The system watches these sources because this market requires them: the
    parties and mediators whose behavior decides the market, wire services,
    the market's stated resolution source, and Google News discovery queries
    built from the decisive-event vocabulary. Requires a rule analysis --
    'no authoritative source plan -> no autonomous entry' is enforced by
    refusing to build a plan for an unanalyzed market.
    """
    analysis = context.rule_analysis
    if analysis is None:
        raise ValueError(f"market {context.market_id} has no rule analysis; refusing to build a source plan")

    rationale: dict[str, list[str]] = {}
    actors = sorted(set(analysis.parties) | set(analysis.mediators))
    actor_domains = official_domains(actors)
    for domain in actor_domains:
        rationale.setdefault(domain, []).append("official domain of a deciding party/mediator")
    for domain in WIRE_DOMAINS:
        rationale.setdefault(domain, []).append("wire service; decisive events are wire-reported")

    resolution_domain = _domain_from_source(context.resolution_source)
    if resolution_domain:
        rationale.setdefault(resolution_domain, []).append("resolution source named by the market")

    # Discovery feeds: one Google News query per (actors x event vocabulary)
    # theme, plus one wire-scoped query. Stable public RSS does not exist for
    # most wires, so Google News RSS is the discovery layer (same approach as
    # the hand-built Iran/Qatar configs).
    actor_terms = [actor.replace("_", " ") for actor in (analysis.parties or actors)][:4]
    keyword_terms = analysis.keywords[:6]
    queries: list[str] = []
    if actor_terms and keyword_terms:
        queries.append(" ".join(actor_terms[:2] + keyword_terms[:2]))
        queries.append(" ".join(actor_terms[:2] + keyword_terms[2:4]) if len(keyword_terms) > 2 else "")
    elif actor_terms:
        queries.append(" ".join(actor_terms[:3]))
    for wire in ("reuters", "AP"):
        if actor_terms:
            queries.append(f"{wire} " + " ".join(actor_terms[:2] + keyword_terms[:1]))
    feed_urls = [google_news_rss(q.strip()) for q in queries if q.strip()]
    for url in feed_urls:
        rationale.setdefault(url, []).append("google news discovery query from parties + decisive-event vocabulary")

    auto_trade = sorted(set(WIRE_DOMAINS) | set(actor_domains) | ({resolution_domain} if resolution_domain else set()))
    escalate_terms = sorted(set(term.lower() for term in keyword_terms + actor_terms if term))

    return SourcePlan(
        market_id=context.market_id,
        rule_text_sha256=context.rule_text_sha256,
        feed_urls=feed_urls,
        poll_urls=[],
        auto_trade_domains=auto_trade,
        alert_only_domains=["x.com", "twitter.com", "t.me"],
        escalate_terms=escalate_terms,
        rationale=rationale,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _domain_from_source(resolution_source: str) -> str | None:
    text = resolution_source.strip().lower()
    if not text:
        return None
    for token in text.replace(",", " ").split():
        cleaned = token.strip(".,;:()[]\"'")
        if "." in cleaned and not cleaned.startswith("http"):
            parts = cleaned.split(".")
            if len(parts) >= 2 and all(parts):
                return cleaned.removeprefix("www.")
        if cleaned.startswith("http"):
            from urllib.parse import urlparse

            netloc = urlparse(cleaned).netloc
            if netloc:
                return netloc.removeprefix("www.")
    return None
