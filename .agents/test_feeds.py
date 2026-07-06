"""Verify the qatar config's feed sources fetch, filter, and promote."""
import sys
sys.path.insert(0, "/home/tstuv/poly/anthropic-exec-bot")

from polybot.location.config import load_location_config
from polybot.iran.source_fetcher import fetch_feed_articles, promote_feed_article
from pathlib import Path

config = load_location_config(Path("/home/tstuv/poly/anthropic-exec-bot/qatar-sept30-yes-protection.yaml"))
inc, exc = config.sources.feed_include_terms, config.sources.feed_exclude_terms

for feed_url in config.sources.feed_urls:
    label = feed_url.split("/")[2]
    print(f"\n=== {label} ===")
    try:
        articles = fetch_feed_articles(feed_url, "polybot/0.1", include_terms=inc, exclude_terms=exc, limit=5)
    except Exception as exc_:
        print(f"  FETCH ERROR: {exc_}")
        continue
    print(f"  {len(articles)} item(s) pass include/exclude filter")
    for a in articles[:3]:
        print(f"  - [{a.domain}] {a.title[:75]}")
    if articles and "news.google" in feed_url:
        promoted = promote_feed_article(articles[0], "polybot/0.1")
        if promoted is None:
            print("  promotion: FAILED (no publisher URL resolved)")
        else:
            print(f"  promotion: -> [{promoted.domain}] kind={promoted.source_kind} text_len={len(promoted.raw_text)}")
