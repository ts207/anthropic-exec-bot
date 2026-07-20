"""Print the source plan the fleet would build for markets matching a term.

Used to verify feed ordering and auto-trade domains after registry changes:
direct publisher feeds must lead, aggregators must trail, and the decisive
official domains must be auto-trade eligible.

Usage: .venv/bin/python scripts/show_source_plan.py blockade
"""

from __future__ import annotations

import sys
from pathlib import Path

from polybot.discovery.config import load_discovery_config
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore

term = (sys.argv[1] if len(sys.argv) > 1 else "blockade").lower()
config = load_discovery_config(Path("configs/geopolitics/discovery.yaml"))

for context in DiscoveryStore(config.data_dir).all_contexts():
    if term not in (context.question or "").lower():
        continue
    if context.rule_analysis is None:
        print(f"(skipped, not yet graded) {context.question}")
        continue
    plan = build_source_plan(context)
    print(f"\nMARKET: {context.question}   [{context.state}]")
    print("feeds, in polling order:")
    for index, url in enumerate(plan.feed_urls):
        kind = "AGGREGATOR" if ("news.google" in url or "bing.com" in url) else "direct"
        print(f"  {index:2}. [{kind}] {url[:92]}")
    print(f"auto_trade_domains: {plan.auto_trade_domains}")
    print(f"escalate_terms: {plan.escalate_terms}")
    break
else:
    print(f"no graded market matched {term!r}")
