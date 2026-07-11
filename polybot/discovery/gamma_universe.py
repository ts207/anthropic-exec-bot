from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from polybot.config import SETTINGS
from polybot.gamma import market_from_gamma

from .config import UniverseConfig
from .types import MarketContext, OutcomeRecord


def fetch_active_events(
    *,
    limit: int,
    page_size: int = 100,
    gamma_host: str = SETTINGS.gamma_host,
    fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Enumerate active, open Gamma events ordered by liquidity, paginated.

    `fetch` is injectable for tests/offline use; the default performs the
    HTTP call. Returns raw Gamma event dicts.
    """
    fetch = fetch or _http_fetch
    events: list[dict[str, Any]] = []
    offset = 0
    url = f"{gamma_host.rstrip('/')}/events"
    while len(events) < limit:
        page = fetch(
            url,
            {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "order": "liquidity",
                "ascending": "false",
                "limit": min(page_size, limit - len(events)),
                "offset": offset,
            },
        )
        if not page:
            break
        events.extend(item for item in page if isinstance(item, dict))
        if len(page) < page_size:
            break
        offset += len(page)
    return events[:limit]


def _http_fetch(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def is_geopolitical_candidate(event: dict[str, Any], universe: UniverseConfig) -> tuple[bool, str]:
    """Category/tag + question/description keyword filter. Returns (candidate,
    reason) so rejections are explainable in the funnel report."""
    text_parts = [str(event.get("title") or ""), str(event.get("description") or "")]
    for market in event.get("markets") or []:
        if isinstance(market, dict):
            text_parts.append(str(market.get("question") or ""))
    text = "\n".join(text_parts).lower()
    tags = _event_tags(event)

    for term in universe.exclude_keywords:
        if term.lower() in text:
            return False, f"excluded_keyword:{term}"
    if any(tag in universe.include_tags for tag in tags):
        return True, f"tag_match:{','.join(sorted(set(tags) & set(universe.include_tags)))}"
    for term in universe.include_keywords:
        if term.lower() in text:
            return True, f"keyword_match:{term}"
    return False, "no_geopolitical_signal"


def _event_tags(event: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    raw_tags = event.get("tags")
    if isinstance(raw_tags, list):
        for item in raw_tags:
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("slug") or "").strip().lower()
                if label:
                    tags.append(label)
            elif isinstance(item, str):
                tags.append(item.strip().lower())
    category = str(event.get("category") or "").strip().lower()
    if category:
        tags.append(category)
    return tags


def context_from_event(event: dict[str, Any]) -> MarketContext | None:
    """Build the durable context skeleton (identity, outcomes, token mappings,
    rule text + hash, tradeability) from one raw Gamma event. Rule ANALYSIS is
    added separately by the analyzer."""
    markets_raw = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    if not markets_raw:
        return None
    metas = []
    for raw in markets_raw:
        try:
            metas.append((raw, market_from_gamma(event, raw)))
        except ValueError:
            continue
    if not metas:
        return None

    grouped = len(metas) > 1
    event_slug = str(event.get("slug") or "")
    outcomes: list[OutcomeRecord] = []
    for raw, meta in metas:
        label = str(raw.get("groupItemTitle") or "").strip() if grouped else "Yes"
        name = _normalize(label or meta.question)
        outcomes.append(
            OutcomeRecord(
                name=name,
                label=label or meta.question,
                market_slug=meta.market_slug,
                question=meta.question,
                condition_id=meta.condition_id,
                yes_token_id=meta.yes_token_id,
                no_token_id=meta.no_token_id,
                tick_size=meta.tick_size,
                neg_risk=meta.neg_risk,
                last_yes_price=meta.outcome_prices[0] if meta.outcome_prices else None,
                volume=meta.volume,
                liquidity=meta.liquidity,
                active=meta.active,
                closed=meta.closed,
                accepting_orders=meta.accepting_orders,
            )
        )

    if grouped:
        rule_text = str(event.get("description") or "").strip()
        market_id = event_slug
    else:
        _, meta = metas[0]
        rule_text = "\n\n".join(part for part in [meta.description, meta.resolution_source] if part).strip()
        market_id = meta.condition_id or event_slug
    digest = hashlib.sha256(rule_text.encode("utf-8")).hexdigest() if rule_text else ""

    deadline = str(event.get("endDate") or (markets_raw[0].get("endDate") if markets_raw else "") or "")
    now = datetime.now(timezone.utc).isoformat()
    return MarketContext(
        market_id=market_id,
        kind="grouped" if grouped else "binary",
        event_slug=event_slug,
        event_title=str(event.get("title") or ""),
        question=str(event.get("title") or metas[0][1].question),
        deadline_iso=deadline,
        outcomes=outcomes,
        rule_text=rule_text,
        rule_text_sha256=digest,
        rule_version=1,
        resolution_source=str(event.get("resolutionSource") or metas[0][1].resolution_source or ""),
        neg_risk=any(meta.neg_risk for _, meta in metas),
        category=str(event.get("category") or ""),
        tags=_event_tags(event),
        volume=sum(meta.volume for _, meta in metas),
        liquidity=sum(meta.liquidity for _, meta in metas),
        active=any(meta.active for _, meta in metas),
        closed=all(meta.closed for _, meta in metas),
        accepting_orders=any(meta.accepting_orders for _, meta in metas),
        state="DISCOVERED",
        discovered_at=now,
        updated_at=now,
    )


def merge_refresh(existing: MarketContext, fresh: MarketContext) -> MarketContext:
    """Refresh live fields on an existing record; preserve analysis/state
    unless the rule text changed, in which case the analysis is dropped, the
    version bumps, and the market falls back to RULES_REVIEW_REQUIRED (the
    changed-rule-hash execution block)."""
    rule_changed = existing.rule_text_sha256 != fresh.rule_text_sha256
    merged = {
        **existing.as_dict(),
        "outcomes": [o.__dict__ for o in fresh.outcomes],
        "deadline_iso": fresh.deadline_iso,
        "volume": fresh.volume,
        "liquidity": fresh.liquidity,
        "active": fresh.active,
        "closed": fresh.closed,
        "accepting_orders": fresh.accepting_orders,
        "tags": fresh.tags,
        "category": fresh.category,
    }
    if rule_changed:
        merged.update(
            {
                "rule_text": fresh.rule_text,
                "rule_text_sha256": fresh.rule_text_sha256,
                "rule_version": existing.rule_version + 1,
                "rule_analysis": None,
                "state": "RULES_REVIEW_REQUIRED",
                "state_reasons": ["rule_text_changed"],
            }
        )
    return MarketContext.from_dict(merged)


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "_")
