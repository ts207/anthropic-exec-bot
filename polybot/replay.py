from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from polybot.core.execution import DryRunTradingAdapter
from polybot.core.types import Article

# Replay harness: feed archived articles (the JSONL the bots already write for
# every article they fetch) through the FULL decision pipeline -- keyword gate,
# screen tier, confirm passes, quote verification, source policy, execution
# policy, dry-run execution -- without waiting for live events. This is how a
# prompt/threshold change gets validated in minutes instead of weeks.
#
# Notes:
# - Runs in an isolated data dir (<data_dir>/replay/, wiped per run); the
#   production state, holdings, and journals are never touched.
# - dry_run is forced regardless of the config; no order can leave.
# - The article-age gate is disabled: archived articles are old by definition.
# - The classifier is whatever the config says: rule_based replays are free;
#   an anthropic config replays against the live model (that is the point of
#   evaluating a prompt change, and it costs real API calls).

_ARTICLE_FIELDS = {"url", "domain", "title", "published_at", "fetched_at", "raw_text", "hash", "source_kind"}


class _CollectingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def notify(self, message: str, **fields: Any) -> None:
        self.messages.append((message, fields))


def load_replay_articles(path: Path, limit: int = 0) -> list[Article]:
    articles: list[Article] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        data = {key: value for key, value in raw.items() if key in _ARTICLE_FIELDS}
        if not data.get("url") or not data.get("raw_text"):
            continue
        defaults = {"domain": "", "title": "", "published_at": None, "fetched_at": "", "raw_text": "", "hash": ""}
        articles.append(Article(**{**defaults, **data}))
        if limit and len(articles) >= limit:
            break
    return articles


def replay_articles_command(config_path: Path, articles_path: Path, *, limit: int = 0) -> int:
    text = config_path.read_text(encoding="utf-8")
    kind = "location" if ("\nevent:" in text or text.startswith("event:")) else "binary"
    articles = load_replay_articles(articles_path, limit)
    if not articles:
        raise SystemExit(f"no replayable articles found in {articles_path}")
    bot = _build_replay_bot(config_path, kind)
    decisions: list[dict[str, Any]] = []
    for article in articles:
        decision = bot.process_article(article)
        decisions.append(
            {
                "url": article.url,
                "domain": article.domain,
                "title": article.title[:120],
                "action": decision.action,
                "level": decision.level,
                "reason": decision.reason,
            }
        )
    current = bot.store.current()
    summary = {
        "config": str(config_path),
        "kind": kind,
        "replayed": len(articles),
        "actions": dict(Counter(item["action"] for item in decisions)),
        "reasons": dict(Counter(item["reason"].split(":")[0] for item in decisions)),
        "final_state": current.state if current is not None else None,
        "final_held": bot.holdings.held_location(),
        "decisions": decisions,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _build_replay_bot(config_path: Path, kind: str) -> Any:
    adapter = DryRunTradingAdapter(yes_shares=1000.0, no_shares=1000.0)
    if kind == "binary":
        import hashlib

        from polybot.binary.config import load_binary_config
        from polybot.binary.market_verifier import BinaryMarketVerification
        from polybot.binary.runner import BinaryRuleBot

        config = _isolate(load_binary_config(config_path))
        # Offline verification built from the config itself: replay must not
        # depend on Gamma still serving a possibly-resolved market.
        verification = BinaryMarketVerification(
            event_slug=config.market.slug,
            market_question=config.market.question,
            rule_text=config.market.resolution_rules,
            rule_text_sha256=hashlib.sha256(config.market.resolution_rules.encode("utf-8")).hexdigest(),
            condition_id="replay-condition",
            yes_token_id=config.position.expected_yes_token_id or "replay-yes",
            no_token_id=config.position.expected_no_token_id or "replay-no",
            tradeable=True,
            tick_size="0.01",
            neg_risk=False,
        )
        bot = BinaryRuleBot(config=config, market=verification, adapter=adapter)
    else:
        from polybot.location.config import load_location_config
        from polybot.location.runner import LocationProtectionBot

        config = _isolate(load_location_config(config_path))
        bot = LocationProtectionBot(config=config, adapter=adapter)
    notifier = _CollectingNotifier()
    bot.notifier = notifier
    bot.executor.notifier = notifier
    return bot


def _isolate(config: Any) -> Any:
    replay_root = config.data_dir / "replay"
    if replay_root.exists():
        shutil.rmtree(replay_root)
    return replace(
        config,
        data_dir=replay_root / "state",
        logs_dir=replay_root / "logs",
        execution=replace(config.execution, dry_run=True),
        sources=replace(config.sources, max_trade_article_age_hours=0.0),
    )


__all__ = ["replay_articles_command", "load_replay_articles"]
